# Resume here — Cultural Calendar handoff

A standalone guide to pick this project up on another machine. The whole project is a git
repo, so "moving machines" is mostly a clone plus two secrets.

## What this is

An editorial planning tool for The New Yorker tracking culturally significant upcoming items
through end of 2026 across film, TV, theatre, art, music, opera, and NY ballet.

- **Live:** https://culture-calendar.github.io
- **Repo:** `culture-calendar/culture-calendar.github.io` (public)
- **Refreshes itself:** a weekly GitHub Actions cron (Mondays ~10:37 UTC) rebuilds and
  redeploys the page. Nothing needs to run on your machine for the site to stay current.

Read `SKILL.md` for the operating rules (the authoritative reference) and `handover.md` for
the session-by-session history.

## Get it running on a new machine

```bash
git clone https://github.com/culture-calendar/culture-calendar.github.io.git
cd culture-calendar.github.io
python3 -m pip install requests          # only runtime dep; add pytest for tests
export TMDB_API_KEY=<your TMDb v4 read token>   # or put it in ~/.zshrc
python3 -m cultural_calendar             # full refresh → data/toy-calendar.html
python3 -m pytest                        # 22 tests, all offline
```

Preview: `python3 -m http.server 8765 --bind 127.0.0.1` →
`http://127.0.0.1:8765/data/toy-calendar.html`.

Two things that do **not** come with the clone (re-add them on the new machine):

1. **`TMDB_API_KEY`** — your TMDb v4 token, in the shell env / `~/.zshrc`. Without it, film
   shows 0 and everything else still works. Never write the token into project files.
2. **`gh` auth** (only if you'll push/deploy) — `brew install gh && gh auth login` (HTTPS,
   browser). The repo's Actions secret `TMDB_API_KEY` is already set, so CI is unaffected.

## How a refresh / deploy works

- Local: `python3 -m cultural_calendar` regenerates `data/calendar.db` + `data/toy-calendar.html`.
- Deploy: pushing code does **not** auto-deploy; trigger a run with
  `gh workflow run weekly-refresh.yml`, then `gh run watch <id>`. It runs tests → refresh →
  publishes to Pages. The weekly cron does this automatically.
- Health: each run prints a drift warning for any source whose row count leaves its
  `EXPECTED_ROWS` range (`registry.py`).

## Current source coverage (24 sources)

- **Film** — TMDb (US theatrical/limited, top ~50 by popularity, all credited).
- **TV** — TVMaze (premieres/launches only).
- **Theatre** — Broadway.org (canonical) + IBDB (forward-looking) deduped; Playbill
  Off-Broadway + BAM. `playbill_broadway` yields 0 by design.
- **Art** — Met, MoMA, Whitney, Brooklyn, MOCA, LACMA, Pace, Gagosian, Guggenheim, New
  Museum, Frick.
- **Music** — Metacritic albums; NY Phil (CloudFront API) + Carnegie (Algolia) concerts,
  NYC-only. Rendered as Music · Concerts and Music · Albums.
- **Opera/Ballet** — Met Opera, NYCB.

## Capture-fixture tier (refresh by hand, occasionally)

Most sources are fully scriptable and need no attention. A few sources keep a committed
fixture because they block scripted fetches:

- **MoMA**, **Frick** — bot-protected against automated browsers; refresh via Claude-in-Chrome
  using the snippets in `cultural_calendar/capture/README.md`. Low frequency (seasonal).
- **The Met** (museum) — `metmuseum.org` 429s datacenter/CI IPs, so it can't fetch in GitHub
  Actions. It self-refreshes its fixture (`met_capture/met-exhibitions.json`) any time the
  calendar runs from a non-blocked IP — i.e. **just run `python3 -m cultural_calendar` locally
  now and then, and commit the updated fixture** to keep the Met current on the live page.
- **Met Opera** — same story: `metopera.org` serves CI/datacenter IPs a JS shell that parses
  to 0 links (this is why the live page lost the whole opera season). It self-refreshes
  `met_opera_capture/met-opera-season.json` from any non-blocked IP and falls back to it in CI,
  so a local run + commit keeps the season on the live page.

## Recent work (this session)

- Removed Playwright; NY Phil became a JSON API; Carnegie via Algolia.
- Behavior-preserving package refactor (four-tactic source model + registry + pytest).
- Published to GitHub Pages under a dedicated org so the URL carries no personal name; weekly
  cron + Node-24 action pins.
- Subtle parchment editorial theme; enlarged masthead; Music split into Concerts/Albums.
- TMDb tightened (US theatrical gate, credit every kept film) — fixed foreign-noise titles
  and missing credits.
- Added IBDB as the forward-looking Broadway source (doubled fall coverage), deduped against
  Broadway.org; cleaned the theatre credit extractor's trailing-word bleed.
- Met CI-429 fixture fallback; `fetch_text` now retries 429s.

## Open threads / deferred

- **Not yet wired (art):** Zwirner & Hauser & Wirth (Next.js RSC streaming — needs in-browser
  flight-data discovery), Tate, Marian Goodman.
- **Music tours** — deferred; launch data is API-key-gated (Ticketmaster/Songkick/Bandsintown).
- **Podcasts** — deferred; no clean dated feed.
- **Minor:** the shared theatre credit extractor can still bleed an odd trailing word on
  unusual markup; broaden `NAME_STOPWORDS` if more cases appear.

## Context for a Claude session on the new machine

- Project lives in this repo; the canonical rules are in `SKILL.md` — load it before working.
- Security discipline: the TMDb token stays in the env / `~/.zshrc`, never in files; verify
  presence with `[ -n "$TMDB_API_KEY" ]`, never echo it. Carnegie's Algolia key is a public
  referer-restricted client key (fine to keep hardcoded with a comment).
- The reusable skill for any JS/anti-bot source: open it in Claude-in-Chrome, watch its
  network calls to find the backend (API / embedded JSON), then script that endpoint in
  Python with `requests` — fixtures only when there's truly no scriptable path.
