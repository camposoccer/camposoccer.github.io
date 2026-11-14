#!/usr/bin/env python3
"""
Build subscribable calendar feeds for Campolindo High School boys soccer.

Reads the official schedule at campoathletics.com for each team level
(Varsity, Junior Varsity, Frosh/Soph) and writes, into the output folder:

  varsity.ics, jv.ics, frosh-soph.ics   one iCalendar feed per level
  all-levels.ics                        every level in one feed
  schedule.csv                          every game, for the Google Sheet
  schedule.json                         every game, for the subscribe page

A file is only rewritten when the games in it actually change, so the
repository history doubles as a log of schedule changes.

Usage:
  python build_feeds.py --out docs
  python build_feeds.py --out docs --fixtures tests/fixtures   (offline test)
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import html as html_lib
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
SITE = "https://www.campoathletics.com"
SPORT_PAGE = "/sport/soccer/boys/?tab=schedule"
SCHEDULE_ENDPOINT = "/sport/RefreshScheduleStyle3ViewComponent/{team_id}?schoolYear={year}"
TZ = ZoneInfo("America/Los_Angeles")
GAME_LENGTH = timedelta(minutes=105)  # 80-minute game + halftime + warm-up buffer
CALENDAR_COLOR = "#D81B60"  # Google Calendar's "Cherry blossom"; Apple Calendar uses this as a hint

# Known team ids on the site (used if the level menu can't be read).
DEFAULT_LEVELS = {
    "Varsity": "138856",
    "Junior Varsity": "138855",
    "Frosh/Soph": "139603",
}
# Level name on the site -> (file slug, short label used in event titles)
LEVEL_INFO = {
    "Varsity": ("varsity", "Varsity"),
    "Junior Varsity": ("jv", "JV"),
    "Frosh/Soph": ("frosh-soph", "Frosh/Soph"),
}
CAMPO_ADDRESS = "Campolindo High School, 300 Moraga Rd, Moraga, CA 94556"
# Street addresses for opponents' fields (public school addresses, from the
# team parents' carpool sheet). Keyed by the facility name the site shows.
OPPONENT_ADDRESSES = {
    "acalanes": "Acalanes High School, 1200 Pleasant Hill Rd, Lafayette, CA 94549",
    "benicia": "Benicia High School, 1101 Military W, Benicia, CA 94510",
    "bishop o'dowd": "Bishop O'Dowd High School, 9500 Stearns Ave, Oakland, CA 94605",
    "clayton valley": "Clayton Valley Charter High School, 1101 Alberta Way, Concord, CA 94521",
    "las lomas": "Las Lomas High School, 1460 S Main St, Walnut Creek, CA 94596",
    "miramonte": "Miramonte High School, 750 Moraga Way, Orinda, CA 94563",
    "monte vista": "Monte Vista High School, 3131 Stone Valley Rd, Danville, CA 94526",
    "mt. diablo": "Mt. Diablo High School, 2450 Grant St, Concord, CA 94520",
    "northgate": "Northgate High School, 425 Castle Rock Rd, Walnut Creek, CA 94598",
    "richmond": "Richmond High School, 1250 23rd St, Richmond, CA 94804",
    "ygnacio valley": "Ygnacio Valley High School, 755 Oak Grove Rd, Concord, CA 94518",
}
CITY_FIXES = {  # typos that appear on the site
    "Lafyaette": "Lafayette",
    "Lafayaette": "Lafayette",
    "Danvile": "Danville",
}
USER_AGENT = "CampoSoccerCalendar/1.0 (parent-run schedule feed)"


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
@dataclass
class Game:
    level: str          # "Varsity"
    slug: str           # "varsity"
    label: str          # "Varsity" / "JV" / "Frosh/Soph"
    day: date
    start: time | None  # None when the site says TBA
    home: bool
    opponent: str
    facility: str
    city: str
    result: str         # "" when no result yet
    status: str = ""    # "Cancelled" / "Postponed" when the site says so

    @property
    def location(self) -> str:
        if "campolindo" in self.facility.lower():
            return CAMPO_ADDRESS
        key = re.sub(r"\s+(high school|hs)$", "", (self.facility or self.opponent).lower().replace("mt ", "mt. "))
        if key in OPPONENT_ADDRESSES:
            return OPPONENT_ADDRESSES[key]
        place = self.facility or self.opponent
        if not re.search(r"school|field|park|stadium|college|center|complex", place, re.I):
            place = f"{place} High School"
        return f"{place}, {self.city}" if self.city else place

    @property
    def title(self) -> str:
        where = "vs" if self.home else "@"
        t = f"Campo {self.label} Soccer {where} {self.opponent}"
        if self.status:
            t = f"{self.status.upper()} – {t}"
        if self.result:
            t += f" ({self.result})"
        elif self.start is None:
            t += " – Time TBD"
        return t


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------
def current_school_year(today: date) -> str:
    start = today.year if today.month >= 7 else today.year - 1
    return f"{start}-{start + 1}"


def http_get(session: requests.Session, path: str) -> str:
    resp = session.get(SITE + path, timeout=30)
    resp.raise_for_status()
    return resp.text


def discover_levels(session: requests.Session) -> dict[str, str]:
    """Read the level menu (Varsity / Junior Varsity / Frosh/Soph) from the site."""
    try:
        soup = BeautifulSoup(http_get(session, SPORT_PAGE), "html.parser")
        menu = soup.select_one("select.schedule-submenu")
        found = {o.get_text(strip=True): o.get("value") for o in menu.find_all("option")} if menu else {}
        levels = dict(DEFAULT_LEVELS)
        for name in LEVEL_INFO:
            if found.get(name):
                levels[name] = found[name]
            else:
                print(f"! '{name}' is not in the site's level menu; using its known team id", file=sys.stderr)
        extra = [n for n in found if n not in LEVEL_INFO]
        if extra:
            print(f"! The site lists levels this calendar doesn't cover: {', '.join(extra)}", file=sys.stderr)
        return levels
    except requests.RequestException as e:
        print(f"! Could not read level menu ({e}); using known team ids", file=sys.stderr)
    return dict(DEFAULT_LEVELS)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
DATE_TIME_RE = re.compile(r"^(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})\s*/\s*(?P<time>.*)$")
STATUS_WORDS = {"cancel": "Cancelled", "canc": "Cancelled", "postpone": "Postponed", "ppd": "Postponed",
                "resched": "Rescheduled"}


def parse_time(text: str) -> time | None:
    """'7:15 PM', '4 PM', '4:00 p.m.', 'Noon', '19:00' -> time; 'TBA'/'TBD'/'' -> None. Else ValueError."""
    s = re.sub(r"[\s.]", "", text).upper()
    if s in ("", "TBA", "TBD"):
        return None
    if s == "NOON":
        return time(12, 0)
    for fmt in ("%I:%M%p", "%I%p", "%H:%M"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            pass
    raise ValueError(f"unreadable start time '{text}'")


def status_from(text: str) -> str:
    low = text.lower()
    return next((v for k, v in STATUS_WORDS.items() if k in low), "")


def site_statuses(soup) -> dict[tuple[str, str], str]:
    """Cancelled/postponed flags from the structured data the site embeds per game."""
    out = {}
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            j = json.loads(s.string or s.get_text())
        except json.JSONDecodeError:
            continue
        st = (j.get("eventStatus") or "").rsplit("/", 1)[-1]
        word = {"EventCancelled": "Cancelled", "EventPostponed": "Postponed", "EventRescheduled": "Rescheduled"}.get(st)
        if word and j.get("startDate"):
            teams = [html_lib.unescape(html_lib.unescape(j.get(k, {}).get("name", ""))) for k in ("homeTeam", "awayTeam")]
            opp = next((n for n in teams if "campolindo" not in n.lower()), "")
            out[(j["startDate"][:10], re.sub(r"[^a-z0-9]", "", opp.lower()))] = word
    return out


def clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def parse_schedule(html: str, level: str, school_year: str) -> tuple[list[Game], list[str]]:
    """Every row on the site becomes a game, or a problem that stops publishing. Nothing is skipped."""
    slug, label = LEVEL_INFO[level]
    first_year, second_year = (int(y) for y in school_year.split("-"))
    soup = BeautifulSoup(html, "html.parser")
    statuses = site_statuses(soup)
    games: list[Game] = []
    problems: list[str] = []

    for row in soup.find_all("tr"):
        dt_el = row.select_one(".event-date-time")
        if not dt_el:
            continue  # header, ad, or practice row
        raw = clean(dt_el.get_text())
        m = DATE_TIME_RE.match(raw)
        try:
            if not m:
                raise ValueError(f"unreadable date '{raw}'")
            month = datetime.strptime(m["mon"][:3].title(), "%b").month
            year = first_year if month >= 7 else second_year
            day = date(year, month, int(m["day"]))
            start = parse_time(m["time"])
        except ValueError as e:
            problems.append(f"{level}: {e} (row: {raw})")
            continue

        at_vs = clean((row.select_one('[class*="event-at-vs"]') or BeautifulSoup("", "html.parser")).get_text())
        opponent = clean(row.select_one(".event-opponent").get_text()) if row.select_one(".event-opponent") else "TBD"

        site_cell = row.select_one('td[data-label="SITE"]')
        facility_spans = (site_cell or row).select(".event-facility")
        facility = clean(facility_spans[0].get_text()) if facility_spans else ""
        city = clean(facility_spans[1].get_text()) if len(facility_spans) > 1 else ""
        for bad, good in CITY_FIXES.items():
            city = city.replace(bad, good)
        if "campolindo" in facility.lower():
            city = "Moraga, CA"  # the site sometimes shows the wrong city on home games

        result_cell = row.select_one('td[data-label="RESULTS"]')
        result = clean(result_cell.get_text()) if result_cell else ""
        status = statuses.get((day.isoformat(), re.sub(r"[^a-z0-9]", "", opponent.lower())), "") or status_from(result)
        if status or not re.search(r"[A-Za-z0-9]", result):
            result = ""  # "- -" means not played yet; a status word becomes the title prefix

        home = at_vs.upper() == "VS" if at_vs else "campolindo" in facility.lower()
        games.append(Game(level, slug, label, day, start, home, opponent or "TBD", facility, city, result, status))

    games.sort(key=lambda g: (g.day, g.start or time(0)))
    return games, problems


# --------------------------------------------------------------------------
# Output: iCalendar
# --------------------------------------------------------------------------
def ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str) -> str:
    """Fold lines longer than 75 octets, as the iCalendar spec requires."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def utc_stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def event_uid(g: Game, seen: dict[str, int]) -> str:
    base = f"{g.slug}|{g.day.isoformat()}|{g.opponent.lower()}|{'home' if g.home else 'away'}"
    n = seen.get(base, 0)
    seen[base] = n + 1
    digest = hashlib.sha1(f"{base}|{n}".encode()).hexdigest()[:16]
    return f"{digest}-{g.slug}@campo-soccer-calendar"


PLACEHOLDER_START = time(0, 0)            # TBD games: 12:00-3:00 AM, as on the school's own calendar
PLACEHOLDER_LENGTH = timedelta(hours=3)


def campo_fields(g: Game, role: str) -> list[str]:
    """Machine-readable details the independent checker reads instead of parsing titles."""
    out = [f"X-CAMPO-ROLE:{role}", f"X-CAMPO-HOME:{'H' if g.home else 'A'}", f"X-CAMPO-OPPONENT:{ics_escape(g.opponent)}"]
    if g.status:
        out.append(f"X-CAMPO-STATUS:{g.status}")
    if g.result:
        out.append(f"X-CAMPO-RESULT:{ics_escape(g.result)}")
    return out


def build_ics(games: list[Game], cal_name: str, stamp: str) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Campo Soccer Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(cal_name)}",
        "X-WR-TIMEZONE:America/Los_Angeles",
        f"X-WR-CALDESC:{ics_escape('Campolindo boys soccer games, updated daily from ' + SITE + '. Always confirm with the coach.')}",
        f"X-APPLE-CALENDAR-COLOR:{CALENDAR_COLOR}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    seen: dict[str, int] = {}
    for g in games:
        desc = [
            f"Campolindo Boys Soccer – {g.level}",
            "Home game" if g.home else "Away game",
            f"Site: {g.facility}{', ' + g.city if g.city else ''}",
        ]
        if g.start is None:
            desc.append("Start time: TBD (not yet posted by the school)")
        if g.status:
            desc.append(f"{g.status} on the school site")
        if g.result:
            desc.append(f"Result: {g.result}")
        desc.append(f"Official schedule: {SITE}{SPORT_PAGE}")

        lines += ["BEGIN:VEVENT", f"UID:{event_uid(g, seen)}", f"DTSTAMP:{stamp}"]
        if g.start is None:
            lines += [
                f"DTSTART;VALUE=DATE:{g.day.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{(g.day + timedelta(days=1)).strftime('%Y%m%d')}",
                "TRANSP:TRANSPARENT",
            ]
        else:
            start = datetime.combine(g.day, g.start, TZ)
            lines += [f"DTSTART:{utc_stamp(start)}", f"DTEND:{utc_stamp(start + GAME_LENGTH)}"]
        lines += campo_fields(g, "game")
        if g.status == "Cancelled":
            lines.append("STATUS:CANCELLED")
        lines += [
            f"SUMMARY:{ics_escape(g.title)}",
            f"LOCATION:{ics_escape(g.location)}",
            f"DESCRIPTION:{ics_escape(chr(10).join(desc))}",
            "CATEGORIES:Soccer",
            "END:VEVENT",
        ]
        # A TBD game also gets a timed placeholder at the team's usual start, so it sits with the other games.
        if g.start is None:
            main_uid = next(l for l in reversed(lines) if l.startswith("UID:"))[4:]
            start = datetime.combine(g.day, PLACEHOLDER_START, TZ)
            ph_title = f"[Time TBD] Campo {g.label} Soccer {'vs' if g.home else '@'} {g.opponent} Placeholder"
            if g.status:
                ph_title = f"{g.status.upper()} – {ph_title}"
            ph_desc = desc + ["Placeholder at 12:00-3:00 AM, as on the school's own calendar. The school hasn't "
                              "posted the real time yet; this disappears once it does."]
            lines += ["BEGIN:VEVENT", f"UID:placeholder-{main_uid}", f"DTSTAMP:{stamp}", *campo_fields(g, "placeholder"),
                      *(["STATUS:CANCELLED"] if g.status == "Cancelled" else []),
                      f"DTSTART:{utc_stamp(start)}", f"DTEND:{utc_stamp(start + PLACEHOLDER_LENGTH)}",
                      f"SUMMARY:{ics_escape(ph_title)}", f"LOCATION:{ics_escape(g.location)}",
                      f"DESCRIPTION:{ics_escape(chr(10).join(ph_desc))}", "CATEGORIES:Soccer", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


# --------------------------------------------------------------------------
# Output: CSV and JSON
# --------------------------------------------------------------------------
CSV_HEADER = ["Level", "Date", "Day", "Start", "Home/Away", "Opponent", "Location", "Result"]


def build_csv(games: list[Game]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_HEADER)
    for g in games:
        w.writerow([
            g.label,
            g.day.isoformat(),
            g.day.strftime("%a"),
            g.start.strftime("%I:%M %p").lstrip("0") if g.start else "TBD",
            "Home" if g.home else "Away",
            g.opponent,
            g.location,
            g.result,
        ])
    return buf.getvalue()


def build_json(games: list[Game]) -> str:
    rows = []
    for g in games:
        d = asdict(g)
        d["day"] = g.day.isoformat()
        d["start"] = g.start.strftime("%H:%M") if g.start else None
        d["title"] = g.title
        d["location"] = g.location
        rows.append(d)
    return json.dumps(rows, indent=1, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------
# Writing only on change
# --------------------------------------------------------------------------
def without_stamps(text: str) -> str:
    return re.sub(r"^DTSTAMP:.*$", "", text, flags=re.M)


def write_if_changed(path: Path, text: str, ics: bool = False) -> bool:
    if path.exists():
        old = path.read_bytes().decode("utf-8")  # keep CRLF line endings intact
        if (without_stamps(old) == without_stamps(text)) if ics else (old == text):
            return False
    path.write_text(text, encoding="utf-8", newline="")
    return True


def describe_changes(old_json: Path, new_games: list[Game]) -> list[str]:
    """Human-readable list of what changed since the last run."""
    if not old_json.exists():
        return ["First build."]
    try:
        old = json.loads(old_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ["Previous schedule unreadable; rebuilt."]

    def key(r):
        return (r["slug"], r["day"], r["opponent"])

    old_map = {key(r): r for r in old}
    new_map = {key(r): r for r in json.loads(build_json(new_games))}
    notes = []
    for k in sorted(new_map.keys() - old_map.keys()):
        notes.append(f"Added: {new_map[k]['label']} {k[1]} – {new_map[k]['title']}")
    for k in sorted(old_map.keys() - new_map.keys()):
        notes.append(f"Removed: {old_map[k]['label']} {k[1]} – {old_map[k]['title']}")
    for k in sorted(new_map.keys() & old_map.keys()):
        a, b = old_map[k], {**new_map[k]}
        a.setdefault("status", "")
        for field in ("start", "home", "location", "result", "status"):
            if a.get(field) != b.get(field):
                notes.append(f"Changed {field}: {b['label']} {k[1]} vs {k[2]}: {a.get(field)} → {b.get(field)}")
    return notes


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def write_status(out: Path, per_level: dict) -> None:
    """Record every successful check (shown on the parent page; also keeps the repo active off-season)."""
    (out / "status.json").write_text(json.dumps({
        "last_checked": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "games": sum(len(gs) for gs in per_level.values()),
        "levels": {slug: len(gs) for slug, gs in per_level.items()},
    }, indent=1) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="docs", help="output folder (default: docs)")
    ap.add_argument("--year", help="school year like 2026-2027 (default: current)")
    ap.add_argument("--fixtures", help="read saved HTML (<slug>.html) instead of the website")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    school_year = args.year or current_school_year(datetime.now(TZ).date())
    print(f"School year {school_year}")

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    levels = dict(DEFAULT_LEVELS) if args.fixtures else discover_levels(session)

    per_level: dict[str, list[Game]] = {}
    problems: list[str] = []
    previous = json.loads((out / "schedule.json").read_text()) if (out / "schedule.json").exists() else []
    for level, team_id in levels.items():
        slug, label = LEVEL_INFO[level]
        if args.fixtures:
            html = (Path(args.fixtures) / f"{slug}.html").read_text(encoding="utf-8")
        else:
            html = http_get(session, SCHEDULE_ENDPOINT.format(team_id=team_id, year=school_year))
        games, level_problems = parse_schedule(html, level, school_year)
        problems += level_problems

        if not games and not level_problems:
            first, second = (int(y) for y in school_year.split("-"))
            this_season = [r for r in previous if r["slug"] == slug
                           and date(first, 7, 1) <= date.fromisoformat(r["day"]) < date(second, 7, 1)]
            if this_season:
                problems.append(f"{level}: the site now shows no games, but {len(this_season)} were listed this "
                                f"season. The page may have changed; keeping the last good calendar.")
        per_level[slug] = games
        print(f"{level}: {len(games)} games")

    if problems:
        report = ["PROBLEM: " + p for p in problems] + ["Nothing was published; the calendars keep their last good version."]
        print("\n".join(report), file=sys.stderr)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                f.write("### Campo soccer schedule check FAILED\n\n" + "\n".join(f"- {r}" for r in report) + "\n")
        return 1

    all_games = sorted((g for gs in per_level.values() for g in gs),
                       key=lambda g: (g.day, g.start or time(0), g.slug))
    write_status(out, per_level)
    if not all_games:
        print("No games found for any level; leaving existing files untouched.", file=sys.stderr)
        return 0

    changes = describe_changes(out / "schedule.json", all_games)
    stamp = utc_stamp(datetime.now(timezone.utc))
    written = []
    for slug, games in per_level.items():
        label = LEVEL_INFO[next(l for l in LEVEL_INFO if LEVEL_INFO[l][0] == slug)][1]
        season = f"{school_year[:4]}-{school_year[-2:]}"  # e.g. 2026-27
        if write_if_changed(out / f"{slug}.ics", build_ics(games, f"{season} Campo Boys Soccer – {label}", stamp), ics=True):
            written.append(f"{slug}.ics")
    if write_if_changed(out / "all-levels.ics", build_ics(all_games, f"{school_year[:4]}-{school_year[-2:]} Campo Boys Soccer – All Levels", stamp), ics=True):
        written.append("all-levels.ics")
    if write_if_changed(out / "schedule.csv", build_csv(all_games)):
        written.append("schedule.csv")
    if write_if_changed(out / "schedule.json", build_json(all_games)):
        written.append("schedule.json")


    report = ["Updated: " + ", ".join(written)] if written else ["No schedule changes."]
    if written:
        report += changes
    unknown = sorted({f"{g.opponent} ({g.city or 'city not listed'})" for g in all_games
                      if not g.home and g.location not in OPPONENT_ADDRESSES.values()})
    if unknown:
        report.append("ADDRESS NEEDED (shown as school name + city until added to OPPONENT_ADDRESSES): "
                      + "; ".join(unknown))
    print("\n".join(report))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("### Campo soccer schedule check\n\n" + "\n".join(f"- {r}" for r in report) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
