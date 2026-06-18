# Cultural Calendar Handover

Last updated: 2026-06-18

---

## CURRENT STATE — 2026-06-18 (read this first; supersedes older sections below)

### Where the code lives and how it ships
- **It is now a git repo** (the "no Git repository" note later in this file is stale).
  GitHub: `culture-calendar/culture-calendar.github.io`, live at
  **https://culture-calendar.github.io**. Local clone: `/Users/henryfinder/Documents/Cultural Calendar 2`.
- **Push** over HTTPS as `hdfinder-tech` via `gh`. (The iMac's SSH keys are
  `hdfinder-tech/frontlist` *deploy keys* and cannot push here — use `gh`.)
- **A push does NOT deploy.** Deploy is a GitHub Action:
  `gh workflow run weekly-refresh.yml --ref main`. CI builds the DB fresh each run
  (`data/` is gitignored), runs tests, renders the HTML, and publishes to Pages.
- **The committed fixtures/caches are the artifact CI consumes** for bot-walled venues
  (see the integrity rule below).

### Engine layout (the package, not `toy_calendar.py`)
- `cultural_calendar/legacy.py` (~3,500 lines) — the engine: every `parse_*`/`import_*`,
  date helpers, credits, `render_html`, the cache/integrity helpers.
- `cultural_calendar/core/config.py` — paths, `Source`, `today()` (Eastern-anchored),
  `end_date()`, `load_sources`, all fixture/cache path constants.
- `cultural_calendar/registry.py` — id → (tactic, importer, `EXPECTED_ROWS` health range).
- `sources.json` — the 35-source list. `cli.py` / `__main__.py` — run loop.
- `tests/` — **46 pytest tests, all offline/deterministic.** `python3 -m pytest`.

### Horizon (changed): rolling 18 months
`config.end_date()` is a **rolling ~18-month** window (end-of-month), floored at 2026-12-31,
overridable with the `CALENDAR_END_DATE` env var (tests pin it for determinism). The old
"hardcoded 2026-12-31" notes below are superseded.

### The integrity rule (central — do not regress)
**A failed/blocked/truncated/rate-limited fetch is STALE (serve the committed fixture or
last-good cache), NEVER empty.** Bot-walled venues 403/429 CI's datacenter IP; we serve their
committed data labeled "stale" rather than zeroing the venue. We deliberately do **not** fight
WAFs with header games — the levers are an off-CI refresh host or permission (below).

### Run commands (current)
```bash
python3 -m cultural_calendar                          # full refresh + render
python3 -m cultural_calendar --source met_exhibitions # one source
python3 -m pytest                                     # 46 tests
CALENDAR_END_DATE=2027-12-31 python3 -m cultural_calendar   # horizon override
MOMA_LIVE=1 python3 -m cultural_calendar --source moma_exhibitions  # MoMA live, non-blocked host only
git push origin main && gh workflow run weekly-refresh.yml --ref main   # deploy (push alone won't)
```
`TMDB_API_KEY` lives in `~/.zshrc` (interactive shells inherit it; CI uses a repo secret;
never write it into files). Carnegie's Algolia key is a public referer-restricted client key
(fine hardcoded, with comment).

### What changed this session (2026-06-18)
1. **The Met museum — recover year-less "–Ongoing" rows.** The Met's *Upcoming* section
   printed "A Lasting Legacy … July 25-Ongoing" with **no year**, so `detect_date_label`
   couldn't anchor it and the row vanished (4 of 5 shown). Fix: inside the `#upcoming`
   section the opening is future by definition, so recover the Month/Day from the article
   text and infer the next occurrence (keeping the raw string as a review note). Now 5 rows;
   committed fixture refreshed. (metmuseum 429s CI → serves the 5-row fixture, "stale".)
2. **TMDb — per-month horizon pass.** A single `popularity.desc` cut capped at 50 clustered
   near-term and dropped low-buzz films 12–18 months out (e.g. Dec-2026: Angry Birds 3,
   Werwulf — both in TMDb). Now: keep the global popularity slate, then walk the horizon
   month by month taking each month's top US theatrical/limited releases, deduped by id.
   Constants `TMDB_GLOBAL_FILMS=50`, `TMDB_PER_MONTH=8`; health range widened to (15, 200).
   **Live count is ~179 films (was 50). OPEN QUESTION (user to decide): tighten/taper the
   density** — options were taper-by-horizon (~110–120), keep flat 179, or flat ~5/month.
3. **MoMA — fixture-backed source + flag-gated live scraper.** moma.org WAF-403s our CI and
   dev hosts. The committed fixture is now the **source of truth** (9 shows). The live
   scraper is behind **`MOMA_LIVE` (default off)** and may override the fixture **only** when
   the fetched doc proves it's the real index: both headings (`Upcoming exhibitions` ..
   `Installations and projects`) + an in-section `/calendar/exhibitions/<id>` link. It parses
   only that section; a 403/shell/unverified parse leaves the fixture untouched; a verified
   fetch refreshes it. Tested against `tests/fixtures/moma_index.html` (**swap this for a
   real saved capture** when you can fetch the index from a non-blocked host).

### "Source runs — 6 of 35 stale or unavailable": what it means
The 6 are **The Met Exhibitions, Metropolitan Opera, MoMA, Brooklyn Museum, Park Avenue
Armory, Serpentine.** "**stale**" here means *served from the committed fixture / last-good
cache because the live fetch from CI's datacenter IP was bot-walled/rate-limited* (MoMA:
deliberately, live disabled). **It does not mean empty or missing** — all six carry current
data (Met 5, Met Opera 22, MoMA 9, Brooklyn 3, Armory 9, Serpentine 1).

- **The count won't fall from code changes** — it tracks CI's IP reputation / WAF behavior,
  which our parser/cache code can't change.
- **Our work improved the content + integrity behind the labels, not the count:** the Met
  went 4→5; Brooklyn went 0→3 (it used to *zero out* on a 429); Met Opera no longer risks a
  zeroed season; MoMA can no longer be emptied or corrupted by a partial parse.
- **Only two levers turn these green:** (a) run the refresh from a **non-blocked host**
  (residential IP — the "off-CI refresh host"), which fetches live, refreshes the committed
  fixtures/caches; then commit + push + deploy; or (b) obtain **permission/allowlisting** from
  the venue. Header games are explicitly out of scope.

### Off-CI refresh model (how to flip stale→fresh content)
On a non-blocked machine: `export TMDB_API_KEY=…` (and optionally `MOMA_LIVE=1`),
`python3 -m cultural_calendar`, then commit the refreshed fixtures/caches, push, and run the
deploy workflow. CI keeps serving whatever was last committed.

### Other handoff docs
`RESUME.md` (longer narrative handoff), `REVIEW.md` (third-party reviewer orientation),
`SKILL.md` (operating skill: rolling-horizon, international-art, live-vs-fixture).

---

## Codebase Architecture (redesigned 2026-06-14)

The monolithic `toy_calendar.py` (2,711 lines) was restructured into the **`cultural_calendar/`
package**. Behavior is preserved (verified by golden-diff: identical item rows except benign
date drift). Layout:

- `core/config.py` — paths, constants, `Source`, horizon (`today`/`end_date`), `load_sources`.
- `core/html.py` — `LinkTextParser`/`ArticleParser`/`MetaParser`, `normalize_space`/`strip_tags`.
- `legacy.py` — all migrated parse_*/import_*/date/principals/discipline/`render_html`/`dedupe_theatre`
  functions (the engine + per-source logic), 1:1 from the old monolith. **Interim:** the finer
  split of `legacy.py` into `core/{dates,db,fetch,principals,discipline,render}.py` +
  `sources/*` is the remaining decomposition step; everything below already works on top of it.
- `sources/base.py` — `SourcePlugin` + the **four fetch tactics** (`json_api`, `html`,
  `embedded_json`, `capture`) + per-source `expected_rows` health check.
- `registry.py` — declarative dispatch: `plugin_for(source)` maps each id → (tactic, importer,
  expected range). Replaces the old scattered `if source.id == ...` chain.
- `cli.py` / `__main__.py` — run loop via the registry + dedupe + render + **health warnings**
  (flags any source whose row count falls outside `EXPECTED_ROWS` — silent-drift alarm).
- `capture/README.md` — the manual **Claude-in-Chrome** refresh procedure for the only two
  capture-tier holdouts, **MoMA** and **Frick** (the JS snippets to run in the connected
  browser + the fixture shapes). **No Playwright** — both sites block automated browsers
  headless *and* headful (MoMA Akamai shell/403, Frick Yottaa 418), so a heavy headless dep
  earned nothing; they're bot-protected against it. NY Phil left the capture tier entirely
  (it's now a JSON API; see below).
- `tests/` — **pytest** (32 tests): date engine, role-credits/discipline, registry+health,
  capture parsers + NY Phil API parser, and the cache-integrity invariants (merge precedence,
  fetch validation, horizon override, live-overrides-cache). All offline/deterministic.
  `toy_calendar.py` is a shim.

**Run commands (unchanged behavior):**
```bash
python3 -m cultural_calendar [--source ID ...] [--reset] [--aperture wide|conservative]
python3 toy_calendar.py ...        # still works (shim)
python3 -m pytest                  # tests
# MoMA/Frick fixtures: refresh by hand via Claude-in-Chrome — see capture/README.md
```

## Session Update — 2026-06-12: Theatre + Music Expansion

Added New York theatre depth and a real music lane, plus parser tuning.

### New sources
- `playbill_offbroadway` (theatre): Off-Broadway aggregator. Keeps only cards with a
  future **opening/preview** signal; closes-only cards (still-running shows) are skipped.
  18 future productions at flagship venues (Public, Atlantic, MCC, Playwrights Horizons,
  Lincoln Center's Newhouse, Lucille Lortel, etc.).
- `bam_programs` (theatre, multi-discipline): parses BAM's machine-readable
  `productionblock` data attributes; keeps discrete future-dated events plus the flagship
  fall festival (Next Wave 2026) as a season planning row. Per-item category mapping
  (Dance→ballet, Music→music).
- `metacritic_albums` (music): **the album lane.** Parses Metacritic's "Upcoming Album
  Release Calendar" — firm dates (Jun→Nov 2026) **and** the anticipated vague-date section
  (Lana Del Rey 2026, Rihanna R9 TBA, Peter Gabriel Dec 2026, …). 167 rows. Notable artists
  (`NOTABLE_MUSIC_ARTISTS`) are boosted, not filtered, so they sort to the top.
- `nyphil_concerts` (music): **the concert lane.** Now a **JSON API source**
  (`import_nyphil_api`): the calendar's own public CloudFront endpoint returns the full
  season already collapsing multi-night runs into one event with a `DateDescription` range.
  NYC-only (`NYPHIL_NON_NYC` drops Bravo! Vail). No browser, no fixture, no month-picker.
- `aoty_upcoming` (music): built first, then **disabled** — AOTY's upcoming feed is a
  near-term firehose with no prominence ranking. Kept as a fallback; Metacritic supersedes.

### Hybrid Off-Broadway decision (locked with user)
Aggregator (Playbill Off-Broadway) for breadth + one reliably-fetchable flagship
institution (BAM). The other flagships were dropped this round: Public Theater 403s,
NYC City Center is JS-loaded, and most institutions' productions already surface through
the Playbill aggregator. They are browser-capturable later if needed.

### Key mechanisms added
- **Full browser headers + curl fallback** in `fetch_text`: several sources (Metacritic)
  TLS-fingerprint `urllib3` and 403 it while serving curl. On a 403 we retry via curl.
- **NY Phil concert lane** via its CloudFront events API (`import_nyphil_api`). Carnegie
  Hall (Algolia) covers the marquee-hall programming but is a noisier NYC-wide aggregator;
  NY Phil's single-institution API is the cleaner concert signal.
- **MoMA fallback fix:** the header upgrade made MoMA return a 200 JS shell instead of a
  403, so the capture fallback now triggers whenever the live parse is empty (not only on
  HTTPError).
- **Theatre dedup** (`dedupe_theatre`): same production from broadway_org + Playbill is
  collapsed, preferring broadway_org's canonical dates.
- **Per-item `category`** honored by `upsert_item` (for multi-discipline BAM).
- **Music render guardrail** mirrors art/theatre: keeps year/season/"TBA" album signals,
  suppresses undated unknown-precision music.

### Parser tuning
- **Chess / already-open fix:** `broadway_already_open` drops any Broadway show whose
  opening date is already past — by date, not by a hand-maintained title list. (Chess
  opened Nov 2025; also why Joe Turner, opened Apr 25 2026, no longer shows as of today.)
- **Met Opera dates normalized:** `met_opera_opening_date` applies 2026-27 season-year
  context (Sep-Dec→2026, Jan-Aug→2027). Fall-2026 operas now sort into the timeline;
  spring-2027 stay label-only.

### People layer + Editorial Picks (added after the TMDb key was set)
- **Principals** now populate `people_json`: film (TMDb `/credits`, director-first), TV
  (TVMaze cast + Creator/EP, gated to score≥40), albums (artist), theatre
  (`extract_theatre_principals` — playwright/director/star from Playbill + Broadway.org
  detail pages), opera (`extract_opera_principals` — composer/director/conductor from Met
  pages). `capture_name_after` bounds names (handles accented spellings, strips trailing
  section words). Coverage: off-Broadway 18/18, Broadway 5/5, Met Opera 16/21, film 60/100,
  TV ~71/105.
- **Render shows a Principals column** in both tables, plus an **Editorial Picks** section:
  top-N per category ranked by `editorial_picks`, which normalizes relevance as a
  **percentile within each source** (raw scores aren't comparable across sources).
- **Known limitation (important):** within-source percentile only discriminates for lanes
  with score variation (film popularity, TV score, boosted albums). Curated lanes assign a
  *constant* importance (off-Bway 12, Bway 20, NY Phil 16), so their percentile is
  degenerate → treated as neutral 50, with raw editorial weight + date breaking ties. Real
  intra-lane ranking needs per-item signals (venue prestige, people prominence). The people
  layer is the prerequisite for that next step.
- **TMDb key** lives in `~/.zshrc` (interactive runs inherit it; sandboxed runs must
  `source ~/.zshrc` first).

### Deferred
- **Music tours** — real launch data is API-key-gated (Ticketmaster Discovery / Songkick /
  Bandsintown) and there is no key in this environment. Needs a key (env var, like
  `TMDB_API_KEY`) before a parser is worth building. NYC-only per editorial scope.
- **Album prominence** beyond the notable-artist boost, and **Carnegie/fall NY Phil**
  capture, are the obvious next refinements.

## Session Addendum — 2026-06-14: Presentation Redesign + Art Expansion

### Presentation redesign (SUPERSEDES the "Editorial Picks" + "Principals column" notes above)
The render is now **month → category**, not the picks/table layout described earlier.
- `render_html` groups dated items by month, then by category (Film, Television, Theatre,
  Art, Music, Opera, Dance), ordered by a **hidden** within-source relevance percentile.
  The relevance number, the precision column, and the **Source column are not shown**.
- **Role-aware credit line** via `format_credits` + `{name, role}` in `people_json`:
  "Directed by … · Written by … · With …" (film); "By … · Directed by …" (theatre);
  "Music by … · Directed by … · Cond. …" (opera); "Created by … · With …" (TV). The album
  **Artist role is suppressed** (already in the title) and album `venue_or_platform` is blank.
- **TV credits = Creator (pilot writer) + leads only.** Executive Producer is dropped (too
  generic); an explicitly typed "Showrunner" is kept if present (TVMaze exposes none today).
- **Music renders as a two-column compact list** (date + title) — albums are mostly date+title.
- **"On the horizon"** holds undated future signals (anticipated albums, season-labeled and
  undated-upcoming exhibitions).

### Art expansion (museums + galleries) — future-opening discipline kept (per user)
- New `MUSEUMS` config + `parse_museum_listing` + `hydrate_museum_dates` +
  `extract_exhibition_window` (detail-hydrating), plus `parse_museum_json` for venues that
  embed exhibition JSON with ISO dates. Wired: **Whitney** (`/exhibitions/upcoming`),
  **Brooklyn Museum** (`/exhibitions/upcoming`), **MOCA LA**, **Pace Gallery**, **New
  Museum** (`/upcoming-exhibitions/`, `json:True`) — plus existing Met + MoMA. ~35 upcoming.
- **Key pattern (user-flagged):** the dedicated **upcoming URL** often reveals the data even
  when the main JS listing doesn't — sometimes as static HTML, sometimes as embedded JSON
  (New Museum's GraphQL payload: `title`/`startDate`/`link`). When a venue's main page is
  unrevealing, **web-search for its upcoming-exhibitions URL** before assuming browser capture.
- **Upcoming-section handling:** when a listing has `<section id="upcoming">` (Whitney,
  Met-style), scope to it and **keep undated upcoming shows** as "Upcoming" horizon items —
  this is how dateless announcements (e.g. Whitney's Roy Lichtenstein) are captured without
  re-admitting on-view shows. Titles come from the detail page `og:title`.
- **Galleries are NY-only** (locked with user). Multi-location galleries filter to NY.
- Lesson (user-flagged): scrape each museum's **upcoming** listing, not the default
  (current) page — that was the Brooklyn 0-rows bug.

### Open / blocked captures (need the browser-capture pattern; NY-only for galleries)
- **Carnegie Hall concerts — SOLVED as a direct-API source (`import_carnegie`, automatable).**
  The events are an **Algolia** index (`CARNEGIE_ALGOLIA`: host `q0tmlopf1j-dsn.algolia.net`,
  appId `Q0TMLOPF1J`, public referer-restricted key, index `prod_Events`). Query it from
  Python by POSTing to the **multi-query** endpoint `/1/indexes/*/queries` with a
  `{"requests":[{indexName, params}]}` body, a `startdate` numericFilters range, **and a
  `Referer: https://www.carnegiehall.org/` header** — returns 346 events incl. the full fall
  season. Filter facility to Stern/Perelman + Zankel and drop rental-mill `licenseename`s
  (`CARNEGIE_RENTAL_MILLS`) → ~105 curated concerts. (The earlier 403 was the wrong endpoint
  — single-index `/query` instead of `/queries`.) No browser at refresh time. The browser is
  only needed to *rediscover* the appId/key if Carnegie rotates them (method:
  `facetedEventSearch` Alpine component → its Algolia client → `client.transporter.headers`).
  The old `carnegie_capture/` fixture + `parse_carnegie_capture` are now superseded (dead).
- **Lincoln Center / Great Performers** not wired — concerts are NY Phil only (June-seeded).
- **JS/blocked art venues pending** (browser visits confirmed data is reachable, but a clean
  lane needs per-site capture — upcoming page, `og:title`, opening-vs-closing date, NY filter):
  Frick, Tate Modern, LACMA; Marian Goodman needs European-date parsing.
  **Gagosian: now wired** as a scriptable Next.js source (`import_gallery_nextdata`,
  `GALLERIES` config — parses `__NEXT_DATA__.props.pageProps.exhibitions` from
  `/exhibitions/upcoming/`, NY-only, future-opening; 1 NY upcoming now since most Gagosian
  NY shows are currently on view). **New Museum: wired** via its upcoming JSON.
  **Guggenheim — now wired** (`import_guggenheim`): it's WordPress (REST type `exhibition`
  exists), and the listing page embeds exhibitions as JSON with structured
  `dates.start {day,month,year}` under `on_view`/`upcoming`. Parsed directly, future-opening
  only — scriptable, no browser. (1 upcoming now: Taryn Simon; the rest are on view.)
  **Frick — wired as a browser-capture source** (`parse_frick_capture`, fixture
  `frick_capture/frick-exhibitions.json`). It's Drupal behind **Yottaa** anti-bot: every
  frick.org path (incl. `/jsonapi`) returns 418 to non-browser clients, so it can't be
  fetched by the importer — capture the UPCOMING section via the browser (get_page_text on
  `/exhibitions`). 2 upcoming in-horizon (Siena, Kent Monkman). This is the one venue so far
  that is *not* directly scriptable (joins NY Phil / MoMA in the capture tier).
  **LACMA — wired & scriptable** (`import_lacma`): Drupal Views, fetchable directly (no
  anti-bot), listing cards expose `field-start-date`/`field-end-date`; parse the h2 title
  link + dates, future-opening only. 1 now (Fashioning Chinese Women); its `/exhibitions`
  is the only listing, so fall shows appear as LACMA announces them.
  *(Update 2026-06-14: Frick is the lone art holdout still needing a browser; NY Phil left
  the capture tier — see below — and MoMA stays a fixture but refreshes via Claude-in-Chrome.)*
  **Zwirner & Hauser & Wirth — still pending:** Next.js RSC streaming (no classic
  `__NEXT_DATA__`); need the in-browser flight-data/API discovery pass. Then Tate, Marian
  Goodman (European dates).
- **Podcasts — deferred:** no clean dated feed exists, only editorial roundups.

## Refresh Runbook (the calendar must be re-run on a cadence)

**Routine refresh (weekly) — one command, no human step:**
```bash
export TMDB_API_KEY=...        # or keep it in ~/.zshrc
python3 toy_calendar.py        # refreshes all auto sources + regenerates the HTML
```
This fully refreshes, with no manual work, every **Tier-1 (automatable)** source:
film (TMDb), TV (TVMaze), **Carnegie Hall (Algolia)**, **NY Phil (CloudFront events API)**,
albums (Metacritic, via the curl fallback), Off-Broadway + Broadway (Playbill/Broadway.org),
Met/Whitney/Brooklyn/MOCA/New Museum/Pace exhibitions, Met Opera, NYCB.

**Tier-2 (browser-capture, periodic manual refresh):** only **MoMA** and **Frick** now —
both parse saved fixtures (`moma_capture/`, `frick_capture/`) refreshed by hand via
Claude-in-Chrome (`cultural_calendar/capture/README.md`). They have no scriptable path: data
is server-rendered behind bot protection that also blocks automated browsers (headless *and*
headful), so there's no Playwright dependency. NY Phil was promoted to Tier-1 (its CloudFront
backend was the "queryable backend" this note used to predict — confirmed and wired).

**Tier-3 (deferred):** music tours (needs a Ticketmaster key), podcasts (no dated feed).

**The repeatable procedure for a JS/anti-bot source (this is the reusable skill):**
1. Open the page in the connected browser (renders past DataDome/JS).
2. Find where the data actually lives, in priority order:
   a. a dedicated **upcoming URL** serving static HTML or **embedded JSON** (New Museum);
   b. a **backend API the page calls** — inspect the framework component / network for an
      endpoint + credentials (Carnegie → Algolia: `facetedEventSearch` Alpine component →
      its client → `transporter.headers`).
3. **Script that endpoint directly in Python** (replicate host, key, body format, and any
   `Referer`/headers the API checks). Prefer this over browser capture.
4. Only fall back to a saved browser-capture **fixture** when there is genuinely no API.

**Verification after any refresh (must stay clean):**
```bash
python3 -m py_compile toy_calendar.py
sqlite3 data/calendar.db "select category,count(*) from items group by category;"
# discipline: no stale/current rows leaking into planning
sqlite3 data/calendar.db "select count(*) from items where category='theatre' and date_start is null and (date_label is null or date_precision='unknown');"  -- expect 0
```

**Known drift to watch:** site redesigns break HTML parsers (re-inspect selectors);
embedded API keys can rotate (rediscover in browser). *(Update 2026-06-18: `end_date()` is no
longer hardcoded — it's a rolling ~18-month window, override with `CALENDAR_END_DATE`.)*

## Workspace

Active workspace:

```text
/Users/henryfinder/Documents/Cultural Calendar 2
```

This folder was seeded from the earlier prototype at:

```text
/Users/henryfinder/Documents/Cultural Calendar
```

**(Superseded — see CURRENT STATE at top.)** This workspace is now a git clone of
`culture-calendar/culture-calendar.github.io`; push via `gh`, deploy via `weekly-refresh.yml`.

## Project Goal

Cultural Calendar is a toy importer and review page for upcoming culturally significant items through the end of 2026. It is meant for editorial planning, not comprehensive event listings.

Coverage lanes in scope:

- Film releases
- Television and streaming premieres/new seasons
- Broadway and Off-Broadway openings
- Major museum exhibitions and art events
- Album releases and major music programming
- Opera productions
- New York-focused ballet programming

## Current Main Files

- `toy_calendar.py`: importer, source-specific parsers, SQLite schema, and HTML renderer.
- `sources.json`: source registry.
- `data/calendar.db`: generated SQLite output.
- `data/toy-calendar.html`: generated review calendar.
- `data/raw/`: saved source snapshots.
- `data/details/`: saved detail-page captures.
- `moma_capture/`: MoMA browser-capture fallback.
- `SKILL.md`: operating instructions for future Codex runs.

## Run Commands

Run all enabled sources:

```bash
python3 toy_calendar.py
```

Run selected sources:

```bash
python3 toy_calendar.py --source broadway_org --source met_exhibitions
```

Run with a clean generated database:

```bash
python3 toy_calendar.py --reset
```

Preview the HTML:

```bash
python3 -m http.server 8765 --bind 127.0.0.1
```

Then open:

```text
http://127.0.0.1:8765/data/toy-calendar.html
```

TMDb requires:

```bash
export TMDB_API_KEY="..."
python3 toy_calendar.py --source tmdb_movies
```

Do not write the TMDb token into project files.

## Current Prototype Status

The importer currently supports:

- TMDb movies, using `TMDB_API_KEY`.
- TVMaze future schedule, filtered to start-like TV signals.
- Broadway.org shows plus detail-page dates.
- Playbill Broadway, currently conservative and generating zero rows from current Broadway cards unless a future date is available.
- The Met exhibitions, now section-aware and limited to upcoming exhibitions.
- MoMA exhibitions via browser-capture fallback.
- Met Opera 2026-27 season.
- New York City Ballet seasons.

The rendered calendar separates:

- Dated calendar items.
- Vague seasonal/year planning items.

Important caveat: undated planning should be used only for genuine future vague signals. It must not be a holding pen for current or stale listings.

## Recent Fixes

### Broadway Dates

Broadway.org detail pages are now used for:

- First Preview Date
- Opening Date
- Closing Date

The main calendar date is the Opening Date. Closing Date is stored as `date_end`.

Verified examples:

- `Joe Turner's Come and Gone`: main date is `Apr 25, 2026`; first preview `Mar 30, 2026`; closing `Jul 26, 2026`.
- `Chess`: Broadway.org has opening `Nov 16, 2025` and closing `Jun 21, 2026`; it should not be treated as a 2026 opening.

The renderer excludes out-of-range `date_start` rows from the dated table, so 2025 openings and 2027 openings do not appear as upcoming 2026 dated items.

### Playbill Broadway

Playbill current Broadway cards were creating misleading undated theatre rows because many cards only exposed status or closing dates.

Current policy:

- Skip Playbill cards with `Closes...` signals.
- Do not create undated theatre rows from Playbill cards.
- Only admit Playbill rows when a future opening/start date can be extracted.

Current result from saved Playbill Broadway raw page:

```text
Parsed Playbill items: 0
```

Verified:

```text
undated theatre db rows: 0
```

### The Met Exhibitions

The old parser consumed the whole exhibitions page and pulled current `Through...`, `Ongoing`, and collection-gallery pages into undated planning.

Current policy:

- Start parsing at `<section id="upcoming">`.
- Require a future start date.
- Skip current `Through...` and undated `Ongoing` entries.
- For future open-ended labels such as `July 25th, 2026–Ongoing`, render the opening date as the primary date and store the original label in `description`.

Current Met rows from the saved raw page:

- `Decorative Arts in China, 1000 to the Present` — `2026-07-25`
- `A King’s Carpet: Louis XIV and the Savonnerie` — `2026-09-08`
- `The Genesis Facade Commission: Liu Wei` — `2026-09-17`
- `Krasner and Pollock: Past Continuous` — `2026-10-04`

Verified old/current rows removed:

- `Arts of Africa`
- `The Face of Life: Modern Portraits at The Met`
- other ongoing collection-gallery rows

### MoMA Exhibitions

MoMA direct fetch may be blocked. The importer falls back to `moma_capture/moma-exhibition-links.json`.

Current policy:

- Keep future starts only.
- Drop current `Through...` and `Ongoing` rows.
- Normalize sponsor/program prefixes when the subject is editorially meaningful.

Examples:

- `Hyundai Card FirstLook: Joan Snyder` should render as `Joan Snyder`.
- Current/through rows should not appear in future planning.

### Renderer Guardrails

The HTML renderer now has defensive filters so stale rows do not appear just because an importer missed something.

Current guardrails:

- Art rows without future start dates are suppressed from undated planning when they are unknown, ongoing, or through-only.
- Theatre rows without future start dates are suppressed from undated planning when they are unknown, closing-only, or date-less.

These are guardrails, not a substitute for source-specific parsing.

## Current Verification Queries

Useful checks:

```bash
python3 -m py_compile toy_calendar.py
```

```bash
sqlite3 data/calendar.db "select count(*) from items where category='theatre' and date_start is null and (date_label is null or date_precision='unknown');"
```

Expected:

```text
0
```

```bash
sqlite3 data/calendar.db "select count(*) from items where category='art' and date_start is null and (date_label is null or date_precision='unknown' or lower(coalesce(date_label,'')) like '%ongoing%' or lower(coalesce(date_label,'')) like '%through%');"
```

Expected:

```text
0
```

Check Joe Turner:

```bash
sqlite3 data/calendar.db "select source_id,title,date_start,date_end,date_label,description from items where lower(title) like '%joe turner%';"
```

Expected Broadway.org row:

```text
date_start = 2026-04-25
date_label = Apr 25, 2026
date_end = 2026-07-26
```

## Known Design Constraints

Do not conflate:

- source facts
- normalized dates
- model/editorial enrichment

The schema already supports this separation:

- `items`
- `item_details`
- `item_model_enrichment`

Do not use model-written summaries or judgments as source facts.

## Known Remaining Issues

The prototype is still a toy. The next machine should prioritize:

1. Split `toy_calendar.py` into source-specific modules and tests.
2. Add parser fixtures from `data/raw/` and `moma_capture/`.
3. Add unit tests for date parsing and stale/current suppression.
4. Improve TV importance filtering; TVMaze still includes weak start signals.
5. Add Off-Broadway institution-specific sources.
6. Add music/album sources.
7. Add deduplication across Broadway.org and Playbill/IBDB before re-enabling Playbill as a row source.
8. Improve Met Opera date normalization into real `date_start` values where possible.
9. Revisit NYCB 2027 season rows if the editorial horizon should stop strictly at 2026.

## Important Editorial Principle

The system should surface what is ahead, not what is merely still running.

If a source page says `Ongoing`, `Through...`, or `Closes...`, that is usually evidence to suppress the row unless the row also has a future opening/start signal. This issue has appeared repeatedly in art and theatre. Treat it as a first-class parser requirement.
