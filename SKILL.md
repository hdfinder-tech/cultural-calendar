# Cultural Calendar Project Skill

Use this skill when working on the Cultural Calendar prototype in this workspace.

## Purpose

Cultural Calendar is an editorial planning prototype for The New Yorker. It tracks culturally significant upcoming items through the end of 2026 across film, television, theatre, art, music, opera, and New York-focused ballet.

The goal is not comprehensive listings. The goal is a useful forward-looking editorial horizon: reviews, profiles, Talk pieces, Goings On coverage, and longer-lead planning.

## Current Workspace

Primary files:

- `cultural_calendar/`: the package — `core/` (config, html parsers), `legacy.py` (migrated
  engine + per-source parse/import logic), `sources/base.py` (the four fetch tactics +
  `SourcePlugin`), `registry.py` (declarative id→tactic→importer dispatch + health ranges),
  `cli.py`/`__main__.py` (run loop + render + drift warnings), `capture/README.md`
  (the manual Claude-in-Chrome refresh procedure for MoMA + Frick — no Playwright). See
  handover "Codebase Architecture" for the full map.
- `toy_calendar.py`: back-compat shim → `cultural_calendar.cli`. Run with
  `python3 -m cultural_calendar` or `python3 toy_calendar.py`. Tests: `python3 -m pytest`.
- `tests/`: pytest suite (date engine, credits/discipline, registry/health, capture parsers).
- `sources.json`: source registry.
- `data/calendar.db`: generated SQLite database.
- `data/toy-calendar.html`: generated review page.
- `data/raw/`: saved source snapshots.
- `data/details/`: saved detail-page captures.
- `moma_capture/`: browser-captured MoMA fallback data.
- `source-strategy.md`, `source-registry.md`, `prototype-findings.md`: strategy and source notes.
- `handover.md`: current machine handoff.

## Operating Rules

Preserve source facts separately from interpretation.

- `items` should contain normalized source-derived calendar rows.
- `item_details` should contain scraped detail-page text and metadata.
- `item_model_enrichment` is reserved for future model-generated summaries, people, tags, why-it-matters notes, profile potential, and confidence.

Do not commit secrets. TMDb requires `TMDB_API_KEY` in the environment.

Prefer source-specific parsers over generic link scraping. Generic link scraping has repeatedly caused stale/current pages to enter the planning calendar.

## Date Policy

The calendar is forward-looking. A row should appear in dated items only when it has a future start/opening/release/premiere date through `2026-12-31`.

Vague planning rows are allowed only when there is a genuine future signal such as `Fall 2026`, `Summer 2026`, or `2026` from a future-facing source context.

Do not put current, old, ongoing, or closing-only rows into undated planning. Undated must not mean "currently running and date unknown."

Preserve original date labels when useful, but do not let labels like `Through...`, `Ongoing`, or `Closes...` become primary planning dates.

## Theatre Rules

Broadway.org is the canonical Broadway source for opening dates, but it's a near-term
"now playing / on sale" list. **IBDB (`ibdb`, ibdb.com/shows) is the forward-looking
companion** that surfaces announced 2026–27 productions before they're on sale:

- `import_ibdb` scopes to the page's **"Current & Upcoming"** section (skip the embedded
  "Opening Nights in History" block, which injects historical shows), pre-filters
  carried-over long-runs by slug, then hydrates each production detail for its Opening Date.
- Firm `Mon DD, YYYY` → exact; vague `Mon YYYY` → month precision; TBD/year-only → skipped.
- Keep future openings in horizon; credits via the shared `extract_theatre_principals`.
- `dedupe_theatre` folds IBDB/Broadway.org overlaps into the canonical Broadway.org row
  (priority: broadway_org > ibdb > playbill_* > bam), so IBDB only adds net-new shows.

For Broadway.org specifically:

- Follow Broadway.org detail pages.
- Extract and store First Preview Date, Opening Date, and Closing Date.
- Use Opening Date as the main calendar date.
- Store Closing Date as `date_end`, not `date_start`.

Do not include carried-over long-running Broadway shows merely because they are still running. Examples to exclude:

- `Wicked`
- `Hamilton`
- `The Lion King`
- `Chicago`
- `Aladdin`
- `Hadestown`
- `The Book of Mormon`
- `Operation Mincemeat`
- `Titaníque`

Playbill Broadway current cards are not safe as calendar rows because they often expose current status and closing dates, not opening dates. Do not create undated theatre rows from Playbill cards unless a future opening/start date is extracted from a reliable field.

## Art Rules

Museum pages often mix current exhibitions, ongoing collection galleries, and future exhibitions. Be section-aware.

For The Met:

- Parse only the `Upcoming Exhibitions` section from the exhibitions page.
- Require a future start date.
- Skip current `Through...` and `Ongoing` pages.
- If a future item has an open-ended label such as `July 25th, 2026–Ongoing`, render the opening date as the primary date and preserve the original label as source detail.

For MoMA:

- Direct HTTP may be blocked.
- Use `moma_capture/moma-exhibition-links.json` as the browser-capture fallback when needed.
- Keep future exact/vague openings.
- Drop already-running/current `Through...` and `Ongoing` rows.
- Normalize sponsor/program prefixes where the artist or exhibition subject is the editorial signal, such as `Hyundai Card FirstLook: Joan Snyder` -> `Joan Snyder`.

Additional museums/galleries (`MUSEUMS` config + `parse_museum_listing` + `hydrate_museum_dates`):

- **Newly-opening only** — never list currently-on-view shows (user decision). Scrape each
  venue's **upcoming** listing (e.g. `/exhibitions/upcoming`), not the default current page.
- Hydrate each exhibition detail page for its opening date (`extract_exhibition_window`);
  keep only future openings within the horizon. Title from the detail `og:title`.
- If the listing has `<section id="upcoming">` (Whitney, Met-style), scope to it and **keep
  undated upcoming shows** as "Upcoming" horizon items (e.g. Whitney's Lichtenstein).
- **Galleries are NY-only**; multi-location galleries (Pace, Gagosian, Zwirner) filter to New York.
- Wired: Whitney, Brooklyn Museum, MOCA, Pace. Pending browser capture: Guggenheim,
  Gagosian, Zwirner, Hauser & Wirth, New Museum, Frick, Tate, LACMA, Marian Goodman.

## Presentation Rules

The user-facing render (`render_html`) is **month → category**:

- Group dated items by month, then category; order within a category by a **hidden**
  within-source relevance percentile. Do **not** show the relevance number, the precision
  column, or the source column.
- Each entry shows a **role-aware credit line** (`format_credits`): "Directed by … ·
  Written by … · With …". Suppress the album Artist role (already in the title).
- **TV credits = Creator (pilot writer) + leads only**; drop Executive Producer; keep an
  explicit "Showrunner" if a source provides one.
- **Music renders as two columns** (date + title). Undated future signals go to **"On the
  horizon."**

## Off-Broadway Rules

Off-Broadway uses a hybrid model: the Playbill Off-Broadway aggregator for breadth plus
reliably-fetchable flagship institutions (currently BAM).

- Admit a Playbill Off-Broadway card only when it has a future **opening or preview** date.
- A card showing only `Closes ...` is a still-running show — skip it. Never read an
  opening date off a closing field (`extract_offbroadway_open_date` only honors
  opening/preview verbs).
- BAM: keep discrete future-dated events and the flagship fall festival (Next Wave) as a
  season planning row; drop year-long umbrellas and past/current seasons.

## Broadway Already-Open Rule

Exclude any Broadway show whose opening date is already past — it is a carried-over run,
not an upcoming production. This is enforced by date (`broadway_already_open`), not by the
`CARRIED_OVER_BROADWAY_TITLES` list, which is only a backstop. (Example: Chess opened Nov
2025.)

## Music Rules

Two lanes, both New York-first / nationally-notable, never a firehose.

- **Albums** (`metacritic_albums`): Metacritic's Upcoming Album Release Calendar. Keep firm
  dates and the anticipated vague-date section ("2026", "Dec 2026", "TBA"). Notable artists
  (`NOTABLE_MUSIC_ARTISTS`) are boosted in `importance_score`, not used to filter.
- **Concerts** (`nyphil_concerts`): NY Philharmonic via its public CloudFront events API
  (`import_nyphil_api` → `https://d1c3g0ihb82aph.cloudfront.net/Prod/events/9/2/none/live`,
  no auth/CORS-open; the WAF TLS-fingerprints urllib3 so it goes through `fetch_text`'s curl
  fallback). The API already collapses multi-night runs into one event with a
  `DateDescription` range. **NYC-only** — `NYPHIL_NON_NYC` drops the summer Bravo! Vail
  residency and other out-of-town venues. No browser, fully automatable.
- Tours are **deferred**: launch data is API-key-gated (Ticketmaster/Songkick/Bandsintown);
  no key in this environment.

## Fetching Rules

- `fetch_text` sends a full browser header set and **falls back to curl on a 403** — some
  sources (Metacritic) TLS-fingerprint `urllib3`.
- Browser-capture sources (MoMA, Frick) parse a saved fixture; the MoMA fallback now
  triggers whenever the live parse returns nothing, not only on an HTTP error. There is **no
  Playwright dependency** — these two are bot-protected against automated browsers (headless
  *and* headful), so their fixtures are refreshed by hand via Claude-in-Chrome
  (`capture/README.md`). NY Phil is no longer a capture source — it's a JSON API.

## Refresh & Automation

The calendar must be re-run on a cadence; `python3 toy_calendar.py` is the refresh command
and auto-refreshes nearly everything (see the handover's **Refresh Runbook**). For a
JS/anti-bot source, the repeatable procedure is: open it in the browser, find the data
(dedicated upcoming URL / embedded JSON / a backend API the page calls), then **script that
endpoint directly in Python** — only fall back to a saved fixture when there is no API.
Two worked examples: Carnegie Hall (an Algolia index queried in `import_carnegie`) and NY
Phil (a CloudFront events API in `import_nyphil_api`) — both found by watching the live
page's network calls in Claude-in-Chrome, then scripted with no browser at refresh time.
Only **MoMA** and **Frick** have no scriptable path (bot-protected, server-rendered, and they
block automated browsers too); those keep a fixture refreshed by hand via Claude-in-Chrome
(`capture/README.md`). There is no Playwright/headless dependency. Always re-run the
verification checks below after a refresh.

## TV Rules

TVMaze is a signal feed, not an episode dump.

Keep:

- New series premieres.
- Season premieres.
- Limited-series launches.
- Major streamer/network events.

Drop ordinary mid-season episodes.

## Verification Checklist

After parser changes, run:

```bash
python3 -m py_compile toy_calendar.py
python3 toy_calendar.py --source SOURCE_ID
```

Then inspect the generated database:

```bash
sqlite3 data/calendar.db "select source_id,title,date_start,date_label,date_precision from items where date_start is null order by source_id,title limit 100;"
```

Before handing off, verify:

- No old/current art rows are in undated planning.
- No current/closing-only theatre rows are in undated planning.
- Joe Turner's Come and Gone uses `Apr 25, 2026` as the main date.
- Playbill closing dates are not used as opening dates.
- `data/toy-calendar.html` was regenerated.

## Local Preview

To preview the generated HTML:

```bash
python3 -m http.server 8765 --bind 127.0.0.1
```

Open:

```text
http://127.0.0.1:8765/data/toy-calendar.html
```
