# Cultural Calendar Project Skill

Use this skill when working on the Cultural Calendar in this workspace.

## Purpose

Cultural Calendar is an editorial planning tool for The New Yorker. It tracks culturally
significant upcoming items through the end of 2026 across film, television, theatre, art,
music, opera, and New York-focused ballet.

The goal is not comprehensive listings. It is a useful forward-looking editorial horizon —
reviews, profiles, Talk pieces, Goings On coverage, and longer-lead planning. Favor signal
over volume.

## Workspace

- `cultural_calendar/` — the package:
  - `core/` — config (paths, dates, the `Source` dataclass) and HTML parsers.
  - `legacy.py` — the engine plus all per-source parse/import logic.
  - `sources/base.py` — the four fetch tactics + `SourcePlugin`.
  - `registry.py` — declarative `id → tactic → importer` dispatch and per-source health ranges.
  - `cli.py` / `__main__.py` — run loop, dedupe, render, drift warnings.
  - `capture/README.md` — how to refresh the capture-fixture sources by hand.
- `sources.json` — the source list.
- `tests/` — pytest suite (dates, credits/discipline, registry/health, parsers).
- `data/` — generated: `calendar.db`, `toy-calendar.html`, `raw/`, `details/` (all gitignored).
- `moma_capture/`, `frick_capture/`, `met_capture/`, `met_opera_capture/`, `carnegie_capture/` — committed fixtures.
- `handover.md` — fuller architecture map and session history.
- `toy_calendar.py` — back-compat shim for `python3 -m cultural_calendar`.

Run: `python3 -m cultural_calendar [--source ID] [--reset]`. Tests: `python3 -m pytest`.

## Architecture and automation

Every source maps to one of four fetch **tactics**, dispatched from `registry.py`:

- `json_api` — configured request → JSON (TMDb, TVMaze, Carnegie/Algolia, NY Phil/CloudFront).
- `html` — `fetch_text` → parser, optionally hydrating detail pages (Broadway.org, IBDB,
  Playbill, the museum framework, Met Opera, NYCB, Metacritic, LACMA).
- `embedded_json` — fetch HTML, extract an embedded JSON blob (Gagosian Next.js, Guggenheim
  WordPress JSON, New Museum GraphQL).
- `capture` — read a committed fixture (MoMA, Frick, Park Avenue Armory; Met museum and Met
  Opera also fall back to a self-refreshing fixture when their live `html` parse comes back
  empty on a blocked IP). The Armory is Cloudflare-bot-walled with no scriptable path at all,
  so its `armory_capture/armory-events.json` is hand-maintained — refresh each season from the
  live current-season page via a browser.

**Deployment:** the repo is `culture-calendar/culture-calendar.github.io`, published at
https://culture-calendar.github.io. A weekly GitHub Actions cron (`.github/workflows/
weekly-refresh.yml`) runs the tests, refreshes every source, and redeploys the page.
`TMDB_API_KEY` is a repo Actions secret. Per-source failures are isolated, and a source whose
row count falls outside its `EXPECTED_ROWS` range prints a drift warning.

**The repeatable procedure for a JS or anti-bot source** (this is the core skill): open it in
the browser, find the real data — a dedicated upcoming URL, embedded JSON, or the backend API
the page calls — then script that endpoint directly in Python. Carnegie Hall (Algolia) and NY
Phil (a CloudFront events API) were both cracked this way and need no browser at refresh time.
Only fall back to a committed fixture when there is genuinely no scriptable path.

## Operating rules

- Keep source facts separate from interpretation: `items` holds normalized calendar rows,
  `item_details` holds scraped detail text, `item_model_enrichment` is reserved for future
  model-generated summaries/tags/notes.
- Do not commit secrets. TMDb needs `TMDB_API_KEY` in the environment.
- Prefer source-specific parsers over generic link scraping — generic scraping repeatedly
  let stale/current pages into the calendar.

## Date policy

The calendar is forward-looking. A row appears in dated items only with a future
start/opening/release/premiere date through `2026-12-31`.

Vague planning rows are allowed only on a genuine future signal (`Fall 2026`, `Summer 2026`,
`2026`) from a forward-facing context; they go to **"On the horizon."** Never let a `Through…`,
`Ongoing`, or `Closes…` label become a primary planning date, and never treat "currently
running, date unknown" as undated-future.

## Fetching

`fetch_text` sends a full browser header set, **falls back to curl on a 403** (some sources,
e.g. Metacritic, TLS-fingerprint `urllib3`), and **retries on a 429** honoring `Retry-After`.

## Film (`tmdb_movies`)

TMDb discover, US releases. Require a real US theatrical/limited release
(`with_release_type=2|3`), cap to the top ~50 by popularity, and fetch credits
(director/writer/cast) for every kept film. The cap doubles as a quality gate — the
low-popularity tail is where non-US-relevant noise lives.

## Television (`tvmaze_full_schedule`)

A signal feed, not an episode dump. Keep new-series premieres, season premieres,
limited-series launches, and major streamer/network events. Drop ordinary mid-season episodes.

## Theatre

**Broadway** comes from two sources, deduped by `dedupe_theatre` (priority
`broadway_org > ibdb > playbill_broadway > bam`):

- `broadway_org` is canonical for opening dates but is a near-term "now playing / on sale"
  list. Follow its detail pages; store First Preview / Opening / Closing dates; use Opening as
  the main date and Closing as `date_end`.
- `ibdb` (ibdb.com/shows) is the forward-looking companion — it carries announced 2026–27
  productions before they're on sale. `import_ibdb` scopes to the **"Current & Upcoming"**
  section (skip the embedded "Opening Nights in History" block), pre-filters carried-over
  long-runs, and reads each detail's Opening Date (firm `Mon DD, YYYY` → exact; vague
  `Mon YYYY` → month precision; TBD/year-only → skipped). Credits via `extract_theatre_principals`.

Exclude carried-over long-running shows by **date** (`broadway_already_open`: opening date in
the past = a carried-over run, not upcoming — e.g. Chess opened Nov 2025).
`CARRIED_OVER_BROADWAY_TITLES` (Wicked, Hamilton, The Lion King, Chicago, Aladdin, Hadestown,
The Book of Mormon, Operation Mincemeat, Titaníque, …) is only a backstop.

**Off-Broadway** is hybrid: the Playbill Off-Broadway aggregator for breadth plus
reliably-fetchable flagship institutions (currently BAM).

- Admit a Playbill Off-Broadway card only with a future **opening or preview** date. A card
  showing only `Closes …` is still-running — skip it. Never read an opening date off a closing
  field (`extract_offbroadway_open_date` honors only opening/preview verbs).
- BAM: keep discrete future-dated events and the fall festival (Next Wave) as a season row;
  drop year-long umbrellas and past/current seasons.
- Playbill Broadway cards are not safe as rows (they expose current status / closing dates,
  not openings).

## Art

Museum pages mix current, ongoing-collection, and future exhibitions — always be
section-aware, and list **newly-opening shows only** (never currently-on-view).

- **The Met** (`met_exhibitions`): parse only the `Upcoming Exhibitions` section; require a
  future start; skip `Through…`/`Ongoing`. For an open-ended label (`July 25, 2026–Ongoing`)
  use the opening date and keep the label as detail. `metmuseum.org` 429s datacenter/CI IPs,
  so a successful fetch refreshes `met_capture/met-exhibitions.json` and a blocked fetch falls
  back to it — the Met always appears, and the fixture self-refreshes from any non-blocked IP.
- **MoMA** (`moma_exhibitions`): keep future exact/vague openings; drop `Through…`/`Ongoing`;
  normalize sponsor prefixes to the editorial subject (`Hyundai Card FirstLook: Joan Snyder`
  → `Joan Snyder`). The live page is bot-protected, so it falls back to
  `moma_capture/moma-exhibition-links.json` whenever the live parse is empty.
- **Other museums/galleries** (`MUSEUMS` + `parse_museum_listing` + `hydrate_museum_dates`):
  scrape each venue's **upcoming** listing, hydrate each detail page for the opening date
  (`extract_exhibition_window`, title from `og:title`), keep future-in-horizon. Where a listing
  has `<section id="upcoming">` (Whitney-style), scope to it and keep undated upcoming shows as
  horizon items. **Galleries are NY-only** (Pace, Gagosian filter to New York).
  - Wired: Met, MoMA, Whitney, Brooklyn Museum, MOCA, LACMA, Pace, Gagosian, Guggenheim,
    New Museum, Frick.
  - Not yet wired: Zwirner and Hauser & Wirth (Next.js RSC streaming), Tate, Marian Goodman.

Name exhibitions for the artist/subject; we generally don't care about the curator.

## Music

Two lanes, NYC-first / nationally-notable, never a firehose. The render splits them into
**Music · Concerts** and **Music · Albums** (`CONCERT_MUSIC_SOURCES`).

- **Albums** (`metacritic_albums`): Metacritic's Upcoming Album Release Calendar. Keep firm
  dates and the anticipated vague-date section ("2026", "Dec 2026", "TBA"). Notable artists
  (`NOTABLE_MUSIC_ARTISTS`) are boosted in `importance_score`, not used to filter. The album
  artist is in the title, so it is not repeated as a credit.
- **Concerts**, NYC-only:
  - `nyphil_concerts` — NY Philharmonic via its public CloudFront events API
    (`import_nyphil_api`, through `fetch_text`'s curl fallback). The API pre-collapses
    multi-night runs into one event with a `DateDescription` range; `NYPHIL_NON_NYC` drops the
    summer Bravo! Vail residency and other out-of-town venues.
  - `carnegie_hall` — Carnegie's own programming via direct Algolia query (`import_carnegie`);
    marquee halls only, rental-mill events dropped.

Tours are **deferred** (launch data is API-key-gated: Ticketmaster/Songkick/Bandsintown).

## Opera and ballet

- `met_opera_2026_27` — Met Opera season; season-year date normalization. Like the Met museum,
  `metopera.org` serves CI/datacenter IPs a JS shell that parses to 0 links, so a good fetch
  refreshes `met_opera_capture/met-opera-season.json` and an empty parse falls back to it — the
  season always appears on the live page, and the fixture self-refreshes from any non-blocked IP.
- `nycb_seasons` — New York City Ballet; keep discrete future-dated programs.

## Presentation

The render (`render_html`) is **month → category**, on a styled editorial "sheet":

- Within a category, order by a **hidden** within-source relevance percentile. Do not show the
  relevance number, the precision column, or the source.
- Each entry shows a **role-aware credit line** (`format_credits`): "Directed by … · Written
  by … · With …". TV credits are **Creator (pilot writer) + leads only** — drop Executive
  Producer, but keep an explicit "Showrunner" when a source labels one.
- **Music renders as two columns** (date + title), split into Concerts and Albums.
- Undated future signals go to **"On the horizon."**

## Verification

After parser changes:

```bash
python3 -m py_compile cultural_calendar/legacy.py
python3 -m cultural_calendar --source SOURCE_ID
python3 -m pytest
sqlite3 data/calendar.db "select source_id,title,date_start,date_label,date_precision \
  from items where date_start is null order by source_id,title limit 100;"
```

Before handing off, confirm:

- No old/current/closing-only rows in undated planning (art or theatre).
- Theatre rows use the opening date — never a preview or closing date.
- Broadway and IBDB don't double-list the same production (dedupe works).
- `data/toy-calendar.html` regenerated.

Preview locally: `python3 -m http.server 8765 --bind 127.0.0.1` →
`http://127.0.0.1:8765/data/toy-calendar.html`. Canonical view is the live Pages URL.
