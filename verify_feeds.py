#!/usr/bin/env python3
"""
Independent check of the published calendars against the official school site.

Written separately from build_feeds.py and reads the site a different way: it
uses the structured game data (schema.org JSON-LD) the site embeds in each
level's schedule, not the visible table that build_feeds.py reads, and it reads
the .ics files with the `icalendar` library, not with our own writer. It also
compares against the school's own calendar feeds.

Exit 1 on any disagreement, so the workflow publishes nothing and GitHub emails
the repository owner. A disagreement that involves only the school's own feed
is reported as a warning the first time (that feed can lag the site by a few
hours) and fails the run if it is still there on the next run.

  python verify_feeds.py --feeds docs                      # live site
  python verify_feeds.py --feeds docs --fixtures tests/fixtures --year 2026-2027
"""
from __future__ import annotations

import argparse, html, json, os, re, sys
from collections import Counter
from datetime import date, datetime
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
SCHOOL_FEED_LEVEL = {"Varsity": "Varsity", "Junior Varsity": "JV", "Frosh/Soph": "Frosh/Soph"}
KNOWN_IDS = {"Varsity": "138856", "Junior Varsity": "138855", "Frosh/Soph": "139603"}
STATUS_WORDS = {"EventCancelled": "Cancelled", "EventPostponed": "Postponed", "EventRescheduled": "Rescheduled"}
PENDING_FILE = "school-feed-pending.json"  # school-feed-only disagreements seen on the previous run


def norm(name: str) -> str:
    n = re.sub(r"[^a-z0-9]", "", html.unescape(html.unescape(name or "")).lower())
    return n or "tbd"


def school_year(today: date) -> str:
    y = today.year if today.month >= 7 else today.year - 1
    return f"{y}-{y + 1}"


def season_window(year: str) -> tuple[date, date]:
    a, b = (int(x) for x in year.split("-"))
    return date(a, 7, 1), date(b, 7, 1)


def fmt(g) -> str:
    return f"{g[0]} {g[1] or 'TBD'} {'vs' if g[2] == 'H' else '@'} {g[3]}"


def sort_key(g):
    return (g[0], g[1] or "", g[2], g[3])


def diff(a: Counter, b: Counter):
    """Games in a more often than in b, in date order."""
    return sorted((a - b).elements(), key=sort_key)


# ------------------------------------------------------------ official side
def official_level(page: str):
    """Games from the JSON-LD blocks, plus the number of visible table rows."""
    soup = BeautifulSoup(page, "html.parser")
    games, statuses, problems = [], {}, []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            j = json.loads(s.string or s.get_text())
        except json.JSONDecodeError:
            problems.append("has an unreadable structured-data block")
            continue
        for ev in (j if isinstance(j, list) else [j]):
            if ev.get("@type") != "SportsEvent" or not ev.get("startDate"):
                continue
            start = datetime.fromisoformat(ev["startDate"][:19])
            home, away = ev.get("homeTeam", {}).get("name", ""), ev.get("awayTeam", {}).get("name", "")
            is_home = norm(home) == norm(SCHOOL)
            tba = (start.hour, start.minute) == (0, 0)
            key = (start.date().isoformat(), None if tba else start.strftime("%H:%M"),
                   "H" if is_home else "A", norm(away if is_home else home))
            games.append(key)
            st = STATUS_WORDS.get((ev.get("eventStatus") or "").rsplit("/", 1)[-1])
            if st:
                statuses[key] = st
    rows = len(soup.select(".event-date-time"))
    return games, statuses, rows, problems


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
            if o.get_text(strip=True) in ids and o.get("value"):
                ids[o.get_text(strip=True)] = o.get("value")
    out = {}
    for lvl, tid in ids.items():
        r = s.get(SITE + COMPONENT.format(id=tid, year=year), timeout=30)
        r.raise_for_status()
        out[lvl] = official_level(r.text)
    return out


def school_feed_games(text: str, lvl: str, window: tuple[date, date]):
    """Games from the school's own feed (TBA games appear there at local midnight)."""
    want_title = re.compile(rf"^Boys {re.escape(SCHOOL_FEED_LEVEL[lvl])} Soccer (vs|at) (.+?)(?: \([^()]*\))?$", re.I)
    out = []
    for ev in Calendar.from_ical(text.encode() if isinstance(text, str) else text).walk("VEVENT"):
        m = want_title.match(str(ev.get("SUMMARY", "")).strip())
        if not m:
            continue  # practices, other teams, anything that isn't one of this level's games
        st = ev.decoded("DTSTART")
        if isinstance(st, datetime):
            st = st.astimezone(TZ) if st.tzinfo else st.replace(tzinfo=TZ)
            day, tm = st.date(), (None if (st.hour, st.minute) == (0, 0) else st.strftime("%H:%M"))
        else:
            day, tm = st, None
        if not (window[0] <= day < window[1]):
            continue
        out.append((day.isoformat(), tm, "H" if m.group(1).lower() == "vs" else "A", norm(m.group(2))))
    return out


def fetch_school_feeds(year: str, fixtures: str | None):
    out, warnings = {}, []
    window = season_window(year)
    for lvl, slug in LEVEL_FILES.items():
        try:
            if fixtures:
                f = Path(fixtures) / f"{slug}.school.ics"
                if not f.exists():
                    continue
                out[lvl] = school_feed_games(f.read_text(encoding="utf-8"), lvl, window)
            else:
                r = requests.get(SCHOOL_FEED.format(id=KNOWN_IDS[lvl]), timeout=30,
                                 headers={"User-Agent": "CampoSoccerCalendar-verify/1.0"})
                r.raise_for_status()
                out[lvl] = school_feed_games(r.text, lvl, window)
        except Exception as e:  # the school's feed being down shouldn't block anything
            warnings.append(f"{lvl}: couldn't read the school's own calendar feed ({e})")
    return out, warnings


# ------------------------------------------------------------ our feeds
def feed_level(path: Path):
    """Our feed: games, statuses, problems. Reads the X-CAMPO-* fields our builder writes."""
    cal = Calendar.from_ical(path.read_bytes())
    games, statuses, problems = [], {}, []
    tbd, placeholders, uids = Counter(), Counter(), []
    for ev in cal.walk("VEVENT"):
        uids.append(str(ev.get("UID")))
        title = str(ev.get("SUMMARY", ""))
        loc = str(ev.get("LOCATION", ""))
        opp, where, role = (str(ev.get(k, "")) for k in ("X-CAMPO-OPPONENT", "X-CAMPO-HOME", "X-CAMPO-ROLE"))
        if not opp or where not in ("H", "A") or role not in ("game", "placeholder"):
            problems.append(f"event without game details: {title}")
            continue
        start, home = ev.decoded("DTSTART"), where == "H"
        if opp.lower() not in title.lower():
            problems.append(f"{title}: title doesn't name the opponent '{opp}'")
        if (" vs " in title) != home:
            problems.append(f"{title}: title says {'away' if home else 'home'} but the game is {'home' if home else 'away'}")
        status = str(ev.get("X-CAMPO-STATUS", ""))
        if status and not title.upper().startswith(status.upper()):
            problems.append(f"{title}: {status.lower()} but the title doesn't say so")
        if role == "placeholder":
            if not isinstance(start, datetime) or not title.startswith(("[Time TBD]", f"{status.upper()} – [Time TBD]")):
                problems.append(f"{title}: placeholder must be a timed '[Time TBD] …' event")
            else:
                placeholders[(start.astimezone(TZ).date().isoformat(), where, norm(opp))] += 1
            continue
        if isinstance(start, datetime):
            local = start.astimezone(TZ)
            day, tm = local.date().isoformat(), local.strftime("%H:%M")
            if not ev.decoded("DTEND") > start:
                problems.append(f"{title} on {day}: end is not after start")
        else:
            day, tm = start.isoformat(), None
            tbd[(day, where, norm(opp))] += 1
            if "Time TBD" not in title and not ev.get("X-CAMPO-RESULT"):
                problems.append(f"{title} on {day}: all-day event without 'Time TBD' in the title")
        if not loc:
            problems.append(f"{title} on {day}: no location")
        elif home and HOME_STREET not in loc and SCHOOL.lower() in loc.lower():
            problems.append(f"{title} on {day}: home game not at {HOME_STREET} ({loc})")
        elif not home and HOME_STREET in loc:
            problems.append(f"{title} on {day}: away game placed at {SCHOOL}")
        key = (day, tm, where, norm(opp))
        games.append(key)
        if status:
            statuses[key] = status
    for k in sorted((placeholders - tbd).elements()):
        problems.append(f"placeholder on {k[0]} vs {k[2]} has no matching 'Time TBD' game")
    for k in sorted((tbd - placeholders).elements()):
        problems.append(f"'Time TBD' game on {k[0]} vs {k[2]} has no timed placeholder")
    if len(uids) != len(set(uids)):
        problems.append("duplicate event IDs")
    return games, statuses, problems


# ------------------------------------------------------------ compare
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--feeds", default="docs")
    ap.add_argument("--year")
    ap.add_argument("--fixtures")
    a = ap.parse_args()
    year = a.year or school_year(datetime.now(TZ).date())
    feeds = Path(a.feeds)
    errors, warnings, lines, school_only = [], [], [], []

    try:
        official = fetch_official(year, a.fixtures)
    except requests.RequestException as e:
        print(f"Could not read the school site ({e}); nothing verified.", file=sys.stderr)
        return 1
    school_feeds, w = fetch_school_feeds(year, a.fixtures)
    warnings += w

    per_level_games = Counter()
    for lvl, slug in LEVEL_FILES.items():
        off_games, off_status, table_rows, off_problems = official[lvl]
        errors += [f"{lvl}: the site {p}" for p in off_problems]
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
        feed_games, statuses, feed_problems = feed_level(path)
        errors += [f"{lvl}: {p}" for p in feed_problems]
        want, have = Counter(off_games), Counter(feed_games)
        per_level_games += have
        errors += [f"{lvl}: MISSING from calendar: {fmt(g)}" for g in diff(want, have)]
        errors += [f"{lvl}: NOT on the school site: {fmt(g)}" for g in diff(have, want)]
        for g, st in off_status.items():
            if statuses.get(g, "") != st:
                errors.append(f"{lvl}: {fmt(g)} is {st.lower()} on the site but not marked in the calendar")
        if lvl in school_feeds:
            sf = Counter(school_feeds[lvl])
            school_only += [f"{lvl}: in the school's own calendar feed but not on its schedule page: {fmt(g)}"
                            for g in diff(sf, want)]
            school_only += [f"{lvl}: on the schedule page but not in the school's own calendar feed: {fmt(g)}"
                            for g in diff(want, sf)]
        src = f", school feed {len(school_feeds[lvl])}" if lvl in school_feeds else ""
        lines.append(f"{lvl}: site {len(off_games)} games{src}, calendar {len(feed_games)}")

    allp = feeds / "all-levels.ics"
    if allp.exists() and per_level_games:
        all_games, _, p = feed_level(allp)
        errors += [f"all-levels: {x}" for x in p]
        if Counter(all_games) != per_level_games:
            errors.append("all-levels.ics is not exactly the three team calendars combined")

    # School-feed-only disagreements: warn first, fail if still there next run.
    pending_path = feeds / PENDING_FILE
    try:
        previous = set(json.loads(pending_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        previous = set()
    for msg in school_only:
        (errors if msg in previous else warnings).append(
            msg + (" (still there since the last run)" if msg in previous else " (rechecking next run)"))
    if not a.fixtures:
        pending_path.write_text(json.dumps(sorted(school_only), indent=1) + "\n", encoding="utf-8")

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
