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
        if self.result:
            t += f" ({self.result})"
        elif self.start is None:
            t += " (time TBA)"
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
        levels = {name: tid for name, tid in found.items() if name in LEVEL_INFO and tid}
        if levels:
            return levels
        print("! Level menu not found; using known team ids", file=sys.stderr)
    except requests.RequestException as e:
        print(f"! Could not read level menu ({e}); using known team ids", file=sys.stderr)
    return dict(DEFAULT_LEVELS)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
DATE_TIME_RE = re.compile(
    r"^(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s*/\s*(?P<time>TBA|TBD|\d{1,2}:\d{2}\s*[AaPp][Mm])$"
)


def clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def parse_schedule(html: str, level: str, school_year: str) -> list[Game]:
    slug, label = LEVEL_INFO[level]
    first_year, second_year = (int(y) for y in school_year.split("-"))
    soup = BeautifulSoup(html, "html.parser")
    games: list[Game] = []

    for row in soup.find_all("tr"):
        dt_el = row.select_one(".event-date-time")
        if not dt_el:
            continue  # header, ad, or practice row
        m = DATE_TIME_RE.match(clean(dt_el.get_text()))
        if not m:
            print(f"! Skipping unreadable date '{clean(dt_el.get_text())}' ({level})", file=sys.stderr)
            continue

        month = datetime.strptime(m["mon"], "%b").month
        year = first_year if month >= 7 else second_year
        day = date(year, month, int(m["day"]))
        start = None
        if m["time"].upper() not in ("TBA", "TBD"):
            start = datetime.strptime(m["time"].upper().replace(" ", ""), "%I:%M%p").time()

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
        if not re.search(r"\d", result):
            result = ""  # "- -" means not played yet

        home = at_vs.upper() == "VS" if at_vs else "campolindo" in facility.lower()
        games.append(Game(level, slug, label, day, start, home, opponent or "TBD", facility, city, result))

    games.sort(key=lambda g: (g.day, g.start or time(0)))
    return games


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
            desc.append("Start time: TBA on the school site")
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
        lines += [
            f"SUMMARY:{ics_escape(g.title)}",
            f"LOCATION:{ics_escape(g.location)}",
            f"DESCRIPTION:{ics_escape(chr(10).join(desc))}",
            "CATEGORIES:Soccer",
            "END:VEVENT",
        ]
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
            g.start.strftime("%-I:%M %p") if g.start else "TBA",
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
        a, b = old_map[k], new_map[k]
        for field in ("start", "home", "location", "result"):
            if a.get(field) != b.get(field):
                notes.append(f"Changed {field}: {b['label']} {k[1]} vs {k[2]}: {a.get(field)} → {b.get(field)}")
    return notes


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
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
    previous = json.loads((out / "schedule.json").read_text()) if (out / "schedule.json").exists() else []
    for level, team_id in levels.items():
        slug, label = LEVEL_INFO[level]
        if args.fixtures:
            html = (Path(args.fixtures) / f"{slug}.html").read_text(encoding="utf-8")
        else:
            html = http_get(session, SCHEDULE_ENDPOINT.format(team_id=team_id, year=school_year))
        games = parse_schedule(html, level, school_year)

        if not games:
            # Off-season or a site hiccup: keep whatever this level had before.
            kept = [r for r in previous if r["slug"] == slug]
            print(f"! {level}: no games found; keeping {len(kept)} previous games", file=sys.stderr)
            games = [
                Game(r["level"], r["slug"], r["label"], date.fromisoformat(r["day"]),
                     time.fromisoformat(r["start"]) if r["start"] else None,
                     r["home"], r["opponent"], r["facility"], r["city"], r["result"])
                for r in kept
            ]
        per_level[slug] = games
        print(f"{level}: {len(games)} games")

    all_games = sorted((g for gs in per_level.values() for g in gs),
                       key=lambda g: (g.day, g.start or time(0), g.slug))
    if not all_games:
        print("No games found for any level; leaving existing files untouched.", file=sys.stderr)
        return 0

    changes = describe_changes(out / "schedule.json", all_games)
    stamp = utc_stamp(datetime.now(timezone.utc))
    written = []
    for slug, games in per_level.items():
        label = LEVEL_INFO[next(l for l in LEVEL_INFO if LEVEL_INFO[l][0] == slug)][1]
        if write_if_changed(out / f"{slug}.ics", build_ics(games, f"Campo {label} Boys Soccer", stamp), ics=True):
            written.append(f"{slug}.ics")
    if write_if_changed(out / "all-levels.ics", build_ics(all_games, "Campo Boys Soccer (all levels)", stamp), ics=True):
        written.append("all-levels.ics")
    if write_if_changed(out / "schedule.csv", build_csv(all_games)):
        written.append("schedule.csv")
    if write_if_changed(out / "schedule.json", build_json(all_games)):
        written.append("schedule.json")

    report = ["Updated: " + ", ".join(written)] if written else ["No schedule changes."]
    if written:
        report += changes
    print("\n".join(report))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("### Campo soccer schedule check\n\n" + "\n".join(f"- {r}" for r in report) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
