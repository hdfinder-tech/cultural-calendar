# Cultural Calendar

Toy prototype for testing public-source ingestion before building the real editorial calendar.

## Run

```bash
python3 toy_calendar.py
```

The default TV aperture is wide, which keeps more maybe-relevant items for later filtering. To run a tighter TV pass:

```bash
python3 toy_calendar.py --aperture conservative
```

The script writes:

- `data/calendar.db`: SQLite database
- `data/raw/`: raw source snapshots
- `data/toy-calendar.html`: simple browsable output

The HTML output separates vague planning items from dated items. Seasonal/announced rows such as `Fall 2026` appear under **On the horizon**, so they remain visible for profile and assignment planning even before exact dates are public. The page also offers an Editorial ⇄ Calendar view toggle (month → category vs. day-by-day).

The database also keeps detail-page capture and future model enrichment separate:

- `item_details`: scraped detail-page metadata and text.
- `item_model_enrichment`: empty placeholders for model summaries, people, editorial tags, profile potential, and confidence.

Broadway filtering excludes obvious carried-over long-running shows such as `Wicked`; TV filtering keeps series/season premieres from major platforms, not ordinary mid-season episodes.

MoMA direct HTTP fetching is blocked (bot protection serves a contentless shell/403), so a browser-session capture is saved in `moma_capture/`. When `moma_capture/moma-exhibition-links.json` exists, the importer uses it as a fallback for the MoMA source.

There is **no Playwright dependency**. MoMA and Frick block automated browsers (headless *and* headful) as well as plain fetches, so their fixtures are refreshed by hand through a real, trusted browser session (Claude-in-Chrome). The exact procedure and JS snippets live in [`cultural_calendar/capture/README.md`](cultural_calendar/capture/README.md). Every other source — including NY Phil, now a JSON API — refreshes unattended.

## TMDb

TMDb requires an API bearer token. Set it before running:

```bash
export TMDB_API_KEY="..."
python3 toy_calendar.py --source tmdb_movies
```

Without `TMDB_API_KEY`, the TMDb source is skipped and the rest of the toy calendar still runs.
