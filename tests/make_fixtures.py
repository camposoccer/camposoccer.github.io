"""Write offline copies of the three schedule tables, mirroring the site's markup.

Data captured from campoathletics.com on 2026-11-08 (school year 2026-2027).
"""
from pathlib import Path

HOME = ("Campolindo", "Moraga, CA")

DATA = {
    "varsity": [
        ("Nov 18 / 8:00 PM", "VS", "Monte Vista", HOME),
        ("Dec 02 / 6:00 PM", "AT", "Richmond", ("Richmond", "Richmond, CA")),
        ("Dec 04 / 6:30 PM", "VS", "Head Royce", HOME),
        ("Dec 08 / 7:00 PM", "VS", "Mt. Diablo", HOME),
        ("Dec 10 / 7:15 PM", "AT", "Acalanes", ("Acalanes", "Lafyaette, CA")),
        ("Dec 15 / 7:00 PM", "AT", "Benicia", ("Benicia", "Benicia, CA")),
        ("Jan 05 / 7:15 PM", "VS", "Clayton Valley", HOME),
        ("Jan 07 / 7:15 PM", "VS", "Bishop O'Dowd", HOME),
        ("Jan 09 / TBA", "AT", "Miramonte", ("Miramonte", "Orinda, CA")),
        ("Jan 12 / 7:00 PM", "AT", "Northgate", ("Northgate", "Walnut Creek, CA")),
        ("Jan 14 / 7:00 PM", "VS", "Las Lomas", HOME),
        ("Jan 19 / 7:00 PM", "VS", "Ygnacio Valley", HOME),
        ("Jan 21 / 7:00 PM", "AT", "Mt. Diablo", ("Mt. Diablo", "Concord, CA")),
        ("Jan 26 / 7:15 PM", "VS", "Acalanes", HOME),
        ("Jan 28 / 7:00 PM", "AT", "Clayton Valley", ("Clayton Valley", "Concord, CA")),
        ("Feb 02 / TBA", "VS", "TBD", HOME),
        ("Feb 04 / 7:00 PM", "VS", "Northgate", HOME),
        ("Feb 06 / TBA", "VS", "TBD", HOME),
        ("Feb 09 / 7:00 PM", "AT", "Las Lomas", ("Las Lomas", "Walnut Creek, CA")),
        ("Feb 11 / 7:00 PM", "AT", "Ygnacio Valley", ("Ygnacio Valley", "Concord, CA")),
        ("Feb 13 / 5:00 PM", "VS", "Miramonte", HOME),
    ],
    "jv": [
        ("Nov 18 / 6:30 PM", "VS", "Monte Vista", ("Campolindo", "Danvile, CA")),
        ("Dec 02 / 4:00 PM", "AT", "Richmond", ("Richmond", "Richmond, CA")),
        ("Dec 04 / 4:30 PM", "VS", "Head Royce", HOME),
        ("Dec 08 / 5:30 PM", "VS", "Mt. Diablo", HOME),
        ("Dec 10 / 5:30 PM", "AT", "Acalanes", ("Acalanes", "Lafayette, CA")),
        ("Dec 15 / 5:30 PM", "AT", "Benicia", ("Benicia", "Benicia, CA")),
        ("Jan 05 / 5:30 PM", "VS", "Clayton Valley", HOME),
        ("Jan 07 / 5:30 PM", "VS", "Bishop O'Dowd", HOME),
        ("Jan 09 / TBA", "AT", "Miramonte", ("Miramonte", "Orinda, CA")),
        ("Jan 12 / 5:30 PM", "AT", "Northgate", ("Northgate", "Walnut Creek, CA")),
        ("Jan 14 / 5:30 PM", "VS", "Las Lomas", HOME),
        ("Jan 19 / 5:30 PM", "VS", "Ygnacio Valley", HOME),
        ("Jan 21 / 5:30 PM", "AT", "Mt. Diablo", ("Mt. Diablo", "Concord, CA")),
        ("Jan 26 / 5:30 PM", "VS", "Acalanes", HOME),
        ("Jan 28 / 5:30 PM", "AT", "Clayton Valley", ("Clayton Valley", "Concord, CA")),
        ("Feb 02 / TBA", "VS", "TBD", HOME),
        ("Feb 04 / 5:30 PM", "VS", "Northgate", HOME),
        ("Feb 06 / TBA", "VS", "TBD", HOME),
        ("Feb 09 / 5:30 PM", "AT", "Las Lomas", ("Las Lomas", "Walnut Creek, CA")),
        ("Feb 11 / 5:30 PM", "AT", "Ygnacio Valley", ("Ygnacio Valley", "Concord, CA")),
        ("Feb 13 / 3:00 PM", "VS", "Miramonte", HOME),
    ],
    "frosh-soph": [
        ("Dec 08 / 4:00 PM", "VS", "Mt. Diablo", HOME),
        ("Dec 10 / 4:00 PM", "AT", "Acalanes", ("Acalanes", "Lafayaette, CA")),
        ("Dec 15 / 4:00 PM", "AT", "Benicia", ("Benicia", "Benicia, CA")),
        ("Jan 05 / 4:00 PM", "VS", "Clayton Valley", HOME),
        ("Jan 07 / 4:00 PM", "VS", "Bishop O'Dowd", HOME),
        ("Jan 09 / TBA", "AT", "Miramonte", ("Miramonte", "Orinda, CA")),
        ("Jan 12 / 4:00 PM", "AT", "Northgate", ("Northgate", "Walnut Creek, CA")),
        ("Jan 14 / 4:00 PM", "VS", "Las Lomas", HOME),
        ("Jan 19 / 4:00 PM", "VS", "Ygnacio Valley", HOME),
        ("Jan 21 / 4:00 PM", "AT", "Mt. Diablo", ("Mt. Diablo", "Concord, CA")),
        ("Jan 26 / 4:00 PM", "VS", "Acalanes", HOME),
        ("Jan 28 / 4:00 PM", "AT", "Clayton Valley", ("Clayton Valley", "Concord, CA")),
        ("Feb 02 / TBA", "VS", "TBD", HOME),
        ("Feb 04 / 4:00 PM", "VS", "Northgate", HOME),
        ("Feb 06 / TBA", "VS", "TBD", HOME),
        ("Feb 09 / 4:00 PM", "AT", "Las Lomas", ("Las Lomas", "Walnut Creek, CA")),
        ("Feb 11 / 4:00 PM", "AT", "Ygnacio Valley", ("Ygnacio Valley", "Concord, CA")),
        ("Feb 13 / 1:00 PM", "VS", "Miramonte", HOME),
    ],
}

ROW = """<tr role="row">
 <td data-label="OPPONENT" class="hide-on-small-and-down"></td>
 <td data-label="LOCATION &amp; TIME">
  <span class="event-date-time show-on-small" aria-label="{dt}">{dt}</span><br>
  <span class="event-at-vs-{cls}" aria-label="{word}">{atvs}</span>
  <span class="event-opponent show-on-small" aria-label="{opp}">{opp}</span>
  <span class="event-facility show-on-small hide-on-med-and-up">{fac}</span>
  <span class="event-facility show-on-small hide-on-med-and-up"><small>{city}</small></span>
  <div class="show-on-small hide-on-med-and-up"><span>- -</span></div>
 </td>
 <td data-label="SITE" class="hide-on-small-and-down">
  <span class="event-facility">{fac}</span><br>
  <span class="event-facility"><small>{city}</small></span>
 </td>
 <td data-label="RESULTS" class="hide-on-small-and-down"><span>{result}</span></td>
</tr>
<tr><td colspan="4"><script>googletag.cmd.push(function () {{}});</script></td></tr>
"""


def render(rows, results=None):
    results = results or {}
    body = "".join(
        ROW.format(dt=dt, atvs=atvs, cls="primary" if atvs == "VS" else "default",
                   word="Versus" if atvs == "VS" else "At", opp=opp, fac=fac, city=city,
                   result=results.get(dt, "- -"))
        for dt, atvs, opp, (fac, city) in rows
    )
    return f"""<html><body><table class="table schedule-table"><thead><tr><th>OPPONENT</th>
<th>LOCATION &amp; TIME</th><th>SITE</th><th>RESULTS</th></tr></thead><tbody>{body}</tbody></table>
<table><tbody><tr><td>No practice schedule available at this time.</td></tr></tbody></table></body></html>"""


if __name__ == "__main__":
    out = Path(__file__).parent / "fixtures"
    out.mkdir(exist_ok=True)
    for slug, rows in DATA.items():
        (out / f"{slug}.html").write_text(render(rows), encoding="utf-8")
    print("wrote", ", ".join(sorted(p.name for p in out.glob("*.html"))))
