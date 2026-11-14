#!/usr/bin/env python3
"""
Independent check of the published calendars against the official school site.

Written separately from build_feeds.py and reads the site a different way: it
uses the structured game data (schema.org JSON-LD) the site embeds in each
level's schedule, not the visible table that build_feeds.py reads, and it reads
the .ics files with the `icalendar` library, not with our own writer.

Fails (exit 1) on any disagreement, so the workflow publishes nothing and
GitHub emails the repository owner.

  python verify_feeds.py --feeds docs                      # live site
  python verify_feeds.py --feeds docs --fixtures tests/fixtures --year 2026-2027
"""
from __future__ import annotations

import argparse, html, json, os, re, sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from icalendar import Calendar

SITE = "https://www.campoathletics.com"
SPORT_PAGE = "/sport/soccer/boys/?tab=schedule"
COMPONENT = "/sport/RefreshScheduleStyle3ViewComponent/{id}?schoolYear={year}"
SCHOOL_FEED = "https://mmboltapi.azurewebsites.net/api/v2/events/calendar/2631135/{id}/calendar.ics"
SCHOOL = "Campolindo"
HOME_STREET = "300 Moraga Rd"
TZ = ZoneInfo("America/Los_Angeles")
LEVEL_FILES = {"Varsity": "varsity", "Junior Varsity": "jv", "Frosh/Soph": "frosh-soph"}
KNOWN_IDS = {"Varsity": "138856", "Junior Varsity": "138855", "Frosh/Soph": "139603"}


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", html.unescape(name or "").lower())


def school_year(today: date) -> str:
    y = today.year if today.month >= 7 else today.year - 1
    return f"{y}-{y + 1}"


# ------------------------------------------------------------ official side
def official_level(page: str):
    """Games from the JSON-LD blocks, plus the number of visible table rows."""
    soup = BeautifulSoup(page, "html.parser")
    games, problems = [], []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            j = json.loads(s.string or s.get_text())
        except json.JSONDecodeError:
            problems.append("unreadable JSON-LD block")
            continue
        for ev in (j if isinstance(j, list) else [j]):
            if ev.get("@type") != "SportsEvent":
                continue
            start = datetime.fromisoformat(ev["startDate"][:19])
            home, away = ev.get("homeTeam", {}).get("name", ""), ev.get("awayTeam", {}).get("name", "")
            is_home = norm(home) == norm(SCHOOL)
            opp = away if is_home else home
            tba = start.time().hour == 0 and start.time().minute == 0
            status = (ev.get("eventStatus") or "").rsplit("/", 1)[-1]
            games.append((start.date().isoformat(), None if tba else start.strftime("%H:%M"),
                          "H" if is_home else "A", norm(opp), status))
    rows = len(soup.select(".event-date-time"))
    return games, rows, problems


def school_feed_games(text: str):
    """The school's own calendar feed: TBA games appear there at midnight."""
    out = []
    for ev in Calendar.from_ical(text.encode() if isinstance(text, str) else text).walk("VEVENT"):
        title = str(ev.get("SUMMARY", ""))
        m = re.search(r" (vs|at) (.+)$", title, re.I)
        if not m:
            continue
        st = ev.decoded("DTSTART")
        if isinstance(st, datetime):
            st = st.astimezone(TZ) if st.tzinfo else st
            day, tm = st.date().isoformat(), (None if (st.hour, st.minute) == (0, 0) else st.strftime("%H:%M"))
        else:
            day, tm = st.isoformat(), None
        out.append((day, tm, "H" if m.group(1).lower() == "vs" else "A", norm(m.group(2))))
    return out


def fetch_school_feeds(fixtures: str | None):
    out, warnings = {}, []
    for lvl, slug in LEVEL_FILES.items():
        try:
            if fixtures:
                f = Path(fixtures) / f"{slug}.school.ics"
                if not f.exists():
                    continue
                out[lvl] = school_feed_games(f.read_text(encoding="utf-8"))
            else:
                r = requests.get(SCHOOL_FEED.format(id=KNOWN_IDS[lvl]), timeout=30,
                                 headers={"User-Agent": "CampoSoccerCalendar-verify/1.0"})
                r.raise_for_status()
                out[lvl] = school_feed_games(r.text)
        except Exception as e:  # the school's feed being down shouldn't block publishing
            warnings.append(f"{lvl}: couldn't read the school's own calendar feed ({e})")
    return out, warnings


def fetch_official(year: str, fixtures: str | None):
    if fixtures:
        return {lvl: official_level((Path(fixtures) / f"{slug}.html").read_text(encoding="utf-8"))
                for lvl, slug in LEVEL_FILES.items()}
    s = requests.Session()
    s.headers["User-Agent"] = "CampoSoccerCalendar-verify/1.0"
    ids = dict(KNOWN_IDS)
    menu = BeautifulSoup(s.get(SITE + SPORT_PAGE, timeout=30).text, "html.parser").select_one("select.schedule-submenu")
    if menu:
        for o in menu.find_all("option"):
            if o.get_text(strip=True) in ids:
                ids[o.get_text(strip=True)] = o.get("value")
    out = {}
    for lvl, tid in ids.items():
        r = s.get(SITE + COMPONENT.format(id=tid, year=year), timeout=30)
        r.raise_for_status()
        out[lvl] = official_level(r.text)
    return out


# ------------------------------------------------------------ feed side
TITLE_RE = re.compile(r"^(?:(?P<status>[A-Z]+) – )?(?P<ph>\[Time TBD\] )?Campo .+? Soccer (?P<where>vs|@) "
                      r"(?P<opp>.+?)(?P<tbd> – Time TBD)?(?: Placeholder)?(?: \((?P<result>[^)]*)\))?$")


def feed_level(path: Path):
    """Returns (games, uids, problems, statuses). Placeholder events are checked, not counted as games."""
    cal = Calendar.from_ical(path.read_bytes())
    games, uids, problems, statuses, tbd, placeholders = [], [], [], {}, set(), set()
    for ev in cal.walk("VEVENT"):
        uids.append(str(ev.get("UID")))
        title = str(ev.get("SUMMARY", ""))
        loc = str(ev.get("LOCATION", ""))
        start = ev.decoded("DTSTART")
        m = TITLE_RE.match(title)
        if not m:
            problems.append(f"unreadable title: {title}")
            continue
        home = m.group("where") == "vs"
        if m.group("ph"):
            if not isinstance(start, datetime):
                problems.append(f"{title}: placeholder is not at a time")
            else:
                placeholders.add((start.astimezone(TZ).date().isoformat(), "H" if home else "A", norm(m.group("opp"))))
            continue
        if isinstance(start, datetime):
            local = start.astimezone(TZ)
            day, tm = local.date().isoformat(), local.strftime("%H:%M")
            end = ev.decoded("DTEND")
            if not end > start:
                problems.append(f"{title} on {day}: end is not after start")
        else:
            day, tm = start.isoformat(), None
            tbd.add((day, "H" if home else "A", norm(m.group("opp"))))
            if not m.group("tbd") and not m.group("result"):
                problems.append(f"{title} on {day}: all-day event without 'Time TBD' in the title")
        if not loc:
            problems.append(f"{title} on {day}: no location")
        elif home and HOME_STREET not in loc:
            problems.append(f"{title} on {day}: home game not at {HOME_STREET} ({loc})")
        elif not home and HOME_STREET in loc:
            problems.append(f"{title} on {day}: away game placed at Campolindo")
        key = (day, tm, "H" if home else "A", norm(m.group("opp")))
        games.append(key)
        statuses[key] = (m.group("status") or "").title()
    for k in sorted(placeholders - tbd):
        problems.append(f"placeholder on {k[0]} vs {k[2]} has no matching 'Time TBD' game")
    for k in sorted(tbd - placeholders):
        problems.append(f"'Time TBD' game on {k[0]} vs {k[2]} has no timed placeholder")
    if len(uids) != len(set(uids)):
        problems.append("duplicate event IDs")
    return games, set(uids), problems, statuses


# ------------------------------------------------------------ compare
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--feeds", default="docs")
    ap.add_argument("--year")
    ap.add_argument("--fixtures")
    a = ap.parse_args()
    year = a.year or school_year(datetime.now(TZ).date())
    feeds = Path(a.feeds)
    errors, warnings, lines = [], [], []

    try:
        official = fetch_official(year, a.fixtures)
    except requests.RequestException as e:
        print(f"Could not read the school site ({e}); nothing verified.", file=sys.stderr)
        return 1

    school_feeds, w = fetch_school_feeds(a.fixtures)
    warnings += w
    all_uids, union = set(), set()
    if not any(g for g, _, _ in official.values()):
        warnings.append(f"The school site lists no games for {year} (off-season?); nothing to compare.")
    for lvl, slug in LEVEL_FILES.items():
        off_games, table_rows, off_problems = official[lvl]
        errors += [f"{lvl}: site {p}" for p in off_problems]
        if not off_games:
            if table_rows:
                errors.append(f"{lvl}: the site shows {table_rows} games but no structured data to check them against")
            else:
                warnings.append(f"{lvl}: no games on the site for {year}")
            continue
        if table_rows != len(off_games):
            errors.append(f"{lvl}: the site's table has {table_rows} games but its structured data has {len(off_games)}")
        path = feeds / f"{slug}.ics"
        if not path.exists():
            errors.append(f"{lvl}: {path.name} is missing")
            continue
        feed_games, uids, feed_problems, statuses = feed_level(path)
        errors += [f"{lvl}: {p}" for p in feed_problems]
        all_uids |= uids
        union |= set(feed_games)
        want, have = sorted(g[:4] for g in off_games), sorted(feed_games)
        fmt = lambda g: f"{g[0]} {g[1] or 'TBD'} {'vs' if g[2] == 'H' else '@'} {g[3]}"
        for g in sorted(set(x for x in want if want.count(x) > have.count(x))):
            errors.append(f"{lvl}: MISSING from calendar: {fmt(g)}")
        for g in sorted(set(x for x in have if have.count(x) > want.count(x))):
            errors.append(f"{lvl}: NOT on the school site: {fmt(g)}")
        word = {"EventCancelled": "Cancelled", "EventPostponed": "Postponed", "EventRescheduled": "Rescheduled"}
        for g in off_games:
            if g[4] in word and statuses.get(g[:4], "") != word[g[4]]:
                errors.append(f"{lvl}: {fmt(g)} is {word[g[4]].lower()} on the site but not marked in the calendar")
        if lvl in school_feeds:
            sf = sorted(school_feeds[lvl])
            for g in sorted(set(x for x in sf if sf.count(x) > have.count(x))):
                errors.append(f"{lvl}: in the school's own calendar feed but MISSING from ours: {fmt(g)}")
            for g in sorted(set(x for x in have if have.count(x) > sf.count(x))):
                errors.append(f"{lvl}: in our calendar but NOT in the school's own calendar feed: {fmt(g)}")
        src = f", school feed {len(school_feeds[lvl])}" if lvl in school_feeds else ""
        lines.append(f"{lvl}: site {len(off_games)} games{src}, calendar {len(feed_games)}")

    allp = feeds / "all-levels.ics"
    if allp.exists() and union:
        all_games, all_ids, p, _ = feed_level(allp)
        errors += [f"all-levels: {x}" for x in p]
        if sorted(all_games) != sorted(g for lvl, slug in LEVEL_FILES.items()
                                       if (feeds / f"{slug}.ics").exists()
                                       for g in feed_level(feeds / f"{slug}.ics")[0]):
            errors.append("all-levels.ics is not exactly the three team calendars combined")

    report = lines + [f"WARNING: {w}" for w in warnings] + [f"ERROR: {e}" for e in errors]
    report.append("RESULT: " + ("FAILED — calendars not published" if errors
                                else "all games match the school site" if lines
                                else "nothing to check yet (no games on the site)"))
    print("\n".join(report))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("### Independent check against the school site\n\n" + "\n".join(f"- {r}" for r in report) + "\n")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
