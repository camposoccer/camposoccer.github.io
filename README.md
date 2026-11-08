# Campo Boys Soccer Calendars

Subscribable calendar feeds (ICS, iCalendar format) for Campolindo High School
boys soccer — Varsity, JV (junior varsity) and Frosh/Soph — rebuilt every day
from the official schedule at
<https://www.campoathletics.com/sport/soccer/boys/?tab=schedule>.

Parents subscribe once in Apple or Google Calendar; new games, time changes and
cancellations flow through automatically.

## What gets published (in `docs/`, served by GitHub Pages)

| File | What it is |
|---|---|
| `index.html` | Parent page with Apple / Google subscribe buttons and an upcoming-games list |
| `varsity.ics`, `jv.ics`, `frosh-soph.ics` | One calendar per team |
| `all-levels.ics` | Every level in one calendar |
| `schedule.csv` | Every game as a table, pulled into the Google Sheet with `IMPORTDATA` |
| `schedule.json` | Same data, used by the parent page |

Only public schedule information is published: dates, times, opponents, home/away,
school addresses and final scores. No roster, parent or carpool data.

## How the daily update works

`.github/workflows/update-feeds.yml` runs on GitHub's servers at 19:47 UTC
(11:47 AM Pacific during the season), so no personal computer needs to be on:

1. Reads the level menu on the boys soccer page to find each team's schedule.
2. Fetches each level's schedule for the current school year.
3. Rebuilds the feeds and commits them **only if something changed**. Each
   commit message is a dated record, and the run summary lists what changed
   (added / removed games, new times, scores).

Safeguards:
- If the site can't be reached, the run fails and GitHub emails the repo owner;
  the existing feeds stay as they were.
- If a level shows no games (off-season, or the site is mid-edit), that level
  keeps its previous games instead of going blank.
- Each game keeps a stable ID, so a time change updates the existing calendar
  entry rather than creating a duplicate.
- In the off-season a monthly heartbeat commit keeps GitHub from pausing the
  schedule after 60 quiet days.

Run it by hand any time: **Actions → Update soccer calendar feeds → Run workflow**.

## One-time setup

1. Create a public GitHub repository and push this folder to it.
2. **Settings → Pages**: Source = *Deploy from a branch*, Branch = `main`, folder = `/docs`.
3. **Settings → Actions → General → Workflow permissions**: *Read and write permissions*.
4. **Actions** tab → enable workflows → run *Update soccer calendar feeds* once.

The parent page is then at `https://<github-user>.github.io/<repo>/`.

## Google Sheet

In any Google Sheet, cell A1:

```
=IMPORTDATA("https://<github-user>.github.io/<repo>/schedule.csv")
```

Google refreshes `IMPORTDATA` about once an hour, so the sheet follows the feeds.

## Adjusting

All settings are at the top of `build_feeds.py`:
- `GAME_LENGTH` — how long each calendar entry lasts (default 1 h 45 min).
- `OPPONENT_ADDRESSES` — add a school when a new opponent appears; otherwise the
  calendar shows "<School> High School, <City>" from the site.
- `CITY_FIXES` — corrects city typos on the site.
- To cover girls soccer too, copy the repo and change `SPORT_PAGE` and
  `DEFAULT_LEVELS` (the girls page uses different team ids).

## Testing offline

```
python tests/make_fixtures.py
python build_feeds.py --out /tmp/out --fixtures tests/fixtures --year 2026-2027
```
