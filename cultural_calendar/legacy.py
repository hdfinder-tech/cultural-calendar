#!/usr/bin/env python3
"""
Toy Cultural Calendar importer.

This is intentionally small and dependency-light. It tests whether public APIs
and official pages can feed a normalized calendar without deciding the final app.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

import subprocess

import requests


# Migrated to the cultural_calendar package (behavior-preserving re-org); re-exported here
# so this module stays runnable during the migration.
from cultural_calendar.core.config import *  # noqa: F401,F403
from cultural_calendar.core.config import ROOT, DATA_DIR, RAW_DIR, DETAIL_DIR, DB_PATH, SOURCES_PATH, HTML_PATH, MOMA_CAPTURE_LINKS, MET_CAPTURE, MET_OPERA_CAPTURE, ARMORY_CAPTURE, SERPENTINE_CAPTURE, VA_CACHE, TATE_MODERN_CACHE, TATE_BRITAIN_CACHE, FLV_CACHE, NPG_CAPTURE, GRAND_PALAIS_CAPTURE, POMPIDOU_CAPTURE, MAM_CAPTURE, FRICK_CAPTURE, OCULA_CAPTURE, MARIAN_GOODMAN_CACHE, MONTH_PATTERN, MONTH_RE, MONTH_NUMBERS, Source, today, end_date, load_sources
from cultural_calendar.core.html import normalize_space, strip_tags, LinkTextParser, ArticleParser, MetaParser  # noqa: F401


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    RAW_DIR.mkdir(exist_ok=True)
    DETAIL_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        create table if not exists source_runs (
            id integer primary key,
            source_id text not null,
            source_name text not null,
            fetched_at text not null,
            status text not null,
            message text,
            raw_path text
        );

        create table if not exists items (
            id integer primary key,
            source_id text not null,
            source_name text not null,
            category text not null,
            title text not null,
            date_start text,
            date_end text,
            date_precision text not null default 'unknown',
            date_label text,
            venue_or_platform text,
            city text,
            people_json text not null default '[]',
            source_url text,
            external_id text,
            description text,
            importance_score integer not null default 0,
            created_at text not null,
            unique(source_id, external_id, title, date_label)
        );

        create table if not exists item_details (
            id integer primary key,
            item_external_id text not null,
            source_id text not null,
            fetched_at text not null,
            detail_url text not null,
            raw_path text,
            page_title text,
            meta_description text,
            og_description text,
            extracted_text text,
            unique(source_id, item_external_id)
        );

        create table if not exists item_model_enrichment (
            id integer primary key,
            item_external_id text not null,
            source_id text not null,
            model_name text,
            enriched_at text,
            short_summary text,
            people_json text not null default '[]',
            editorial_tags_json text not null default '[]',
            why_it_matters text,
            profile_potential integer,
            confidence text,
            unique(source_id, item_external_id)
        );
        """
    )
    return conn


def fetch_text(url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> str:
    base_headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        # Full Client-Hints / Fetch-Metadata set; several sources (e.g. Metacritic) 403 a
        # bare client but serve the full page once these browser headers are present.
        "sec-ch-ua": '"Chromium";v="125", "Not.A/Brand";v="24", "Google Chrome";v="125"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    }
    if headers:
        base_headers.update(headers)
    for attempt in range(3):
        try:
            response = requests.get(url, params=params, headers=base_headers, timeout=30)
            response.raise_for_status()
            return response.text
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            # Some sources (e.g. Metacritic) fingerprint the TLS client and 403 urllib3 while
            # serving curl normally. Retry once via curl, which presents a browser-like TLS.
            if status == 403:
                # Some WAFs (Tate) block our Client-Hints UA string (Chrome/125.0.0.0) but
                # serve a plainer one; others (Metacritic) fingerprint urllib3's TLS and need
                # curl. Try a stripped-down request with an alternate UA first, then curl.
                try:
                    retry = requests.get(
                        url, params=params, timeout=30,
                        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                 "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"},
                    )
                    if retry.ok:
                        return retry.text
                except requests.RequestException:
                    pass
                return fetch_with_curl(url, params, base_headers)
            # 429 = rate-limited (e.g. metmuseum.org throttles datacenter IPs like CI runners).
            # Back off (honoring Retry-After when present) and retry, since the refresh is not
            # time-critical. Only the last attempt is allowed to raise.
            if status == 429 and attempt < 2:
                time.sleep(min(_retry_after_seconds(exc.response) or 5 * (attempt + 1), 30))
                continue
            raise
    raise RuntimeError(f"unreachable retry exit for {url}")  # pragma: no cover


def _retry_after_seconds(response: "requests.Response | None") -> int | None:
    """Parse a Retry-After header (delta-seconds form) into an int, if present and sane."""
    if response is None:
        return None
    value = response.headers.get("Retry-After", "").strip()
    return int(value) if value.isdigit() else None


def fetch_with_curl(url: str, params: dict[str, Any] | None, headers: dict[str, str]) -> str:
    full_url = url
    if params:
        full_url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
    # Append the HTTP status so a 403/404/challenge body (which curl returns with exit 0) is
    # rejected instead of flowing back as if it were real content.
    command = ["curl", "-sS", "--compressed", "--max-time", "30", "-w", "\\n%{http_code}", full_url]
    for key, value in headers.items():
        command += ["-H", f"{key}: {value}"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=40)
    if result.returncode != 0:
        raise RuntimeError(f"curl fetch failed for {url}: {result.stderr[:200]}")
    body, _, status_code = result.stdout.rpartition("\n")
    if not status_code.strip().startswith("2"):
        raise RuntimeError(f"curl fetch for {url} returned HTTP {status_code.strip()}")
    return body


def fetch_valid_page(url: str, must_contain: tuple[str, ...] = ()) -> str | None:
    """Fetch a page, returning its text only if it looks like a complete, real page — not a bot
    challenge, a redirect stub, or a truncated shell. Returns None on any failed, blocked, or
    suspicious fetch so callers can keep the last-known-good data. Rule: a failed scrape makes
    data STALE, never empty. `must_contain` asserts expected page boilerplate (page-shape check)."""
    try:
        text = fetch_text(url)
    except Exception:
        return None
    lowered = text.lower()
    if (len(text) < 8000 or "been blocked" in lowered or "attention required" in lowered
            or "just a moment" in lowered or "cf-chl" in lowered or "<title>301" in lowered):
        return None
    if must_contain and not all(token in text for token in must_contain):
        return None
    return text


def merge_by_title(base: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Union two item lists, de-duplicated by normalized title (base wins on conflict)."""
    out = list(base)
    have = {normalized_dedupe_title(i["title"]) for i in base}
    for item in extra:
        key = normalized_dedupe_title(item["title"])
        if key not in have:
            out.append(item)
            have.add(key)
    return out


def museum_cache_path(source_id: str) -> Path:
    """Committed last-good cache for a museum-framework source, self-refreshed on a clean fetch."""
    return ROOT / f"{source_id}_capture" / f"{source_id}.json"


CACHE_MISS_LIMIT = 2  # retire a cache-only row after this many consecutive complete-fetch misses


def age_cache(live: list[dict[str, Any]], cache: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge a COMPLETE clean fetch with its cache, live-first, aging out stale cache-only rows.

    A row the live fetch no longer lists (cancelled, renamed, postponed out, or corrected) is
    kept for CACHE_MISS_LIMIT-1 misses, then dropped — so the cache can't immortalize a row a
    clean source has retired, while a single blocked/partial run can't wipe it. Live rows reset
    the counter. The per-row `_misses` counter rides in the cache JSON and is ignored on upsert.
    Only call on a complete clean fetch (a single-page listing, or all sources succeeded)."""
    live_keys = {normalized_dedupe_title(i["title"]) for i in live}
    out = [dict({k: v for k, v in i.items() if k != "_misses"}, _misses=0) for i in live]
    for row in cache:
        if normalized_dedupe_title(row["title"]) in live_keys:
            continue  # superseded by a fresh live row
        misses = row.get("_misses", 0) + 1
        if misses < CACHE_MISS_LIMIT:
            out.append(dict(row, _misses=misses))  # still within grace; keep but age
    return out


def save_raw(source: Source, text: str) -> Path:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RAW_DIR / f"{timestamp}-{source.id}.txt"
    path.write_text(text)
    return path


def save_detail(source: Source, external_id: str, text: str) -> Path:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_id = re.sub(r"[^a-zA-Z0-9]+", "-", external_id.strip("/"))[-80:].strip("-") or "detail"
    path = DETAIL_DIR / f"{timestamp}-{source.id}-{safe_id}.html"
    path.write_text(text)
    return path


def record_run(conn: sqlite3.Connection, source: Source, status: str, message: str = "", raw_path: Path | None = None) -> None:
    conn.execute(
        """
        insert into source_runs (source_id, source_name, fetched_at, status, message, raw_path)
        values (?, ?, ?, ?, ?, ?)
        """,
        (
            source.id,
            source.name,
            dt.datetime.now(dt.timezone.utc).isoformat(),
            status,
            message,
            str(raw_path) if raw_path else None,
        ),
    )
    conn.commit()


def upsert_item(conn: sqlite3.Connection, source: Source, item: dict[str, Any]) -> None:
    conn.execute(
        """
        insert or ignore into items (
            source_id, source_name, category, title, date_start, date_end,
            date_precision, date_label, venue_or_platform, city, people_json,
            source_url, external_id, description, importance_score, created_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source.id,
            source.name,
            item.get("category") or source.category,
            item.get("title"),
            item.get("date_start"),
            item.get("date_end"),
            item.get("date_precision", "unknown"),
            item.get("date_label"),
            item.get("venue_or_platform"),
            item.get("city"),
            json.dumps(item.get("people", [])),
            item.get("source_url"),
            item.get("external_id"),
            item.get("description"),
            item.get("importance_score", 0),
            dt.datetime.now(dt.timezone.utc).isoformat(),
        ),
    )


def upsert_detail(conn: sqlite3.Connection, source: Source, item: dict[str, Any], html_text: str, raw_path: Path) -> None:
    parser = MetaParser()
    parser.feed(html_text)
    extracted = normalize_space(re.sub(r"<[^>]+>", " ", html_text))[:4000]
    conn.execute(
        """
        insert into item_details (
            item_external_id, source_id, fetched_at, detail_url, raw_path,
            page_title, meta_description, og_description, extracted_text
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(source_id, item_external_id) do update set
            fetched_at=excluded.fetched_at,
            detail_url=excluded.detail_url,
            raw_path=excluded.raw_path,
            page_title=excluded.page_title,
            meta_description=excluded.meta_description,
            og_description=excluded.og_description,
            extracted_text=excluded.extracted_text
        """,
        (
            item.get("external_id"),
            source.id,
            dt.datetime.now(dt.timezone.utc).isoformat(),
            item.get("source_url"),
            str(raw_path),
            parser.title,
            parser.meta.get("description"),
            parser.meta.get("og:description"),
            extracted,
        ),
    )


def ensure_model_enrichment_placeholder(conn: sqlite3.Connection, source: Source, item: dict[str, Any]) -> None:
    conn.execute(
        """
        insert or ignore into item_model_enrichment (item_external_id, source_id)
        values (?, ?)
        """,
        (item.get("external_id"), source.id),
    )


def detect_date_label(text: str) -> tuple[str | None, str | None, str]:
    text = normalize_space(text)
    exact_range = re.search(
        rf"((?:{MONTH_RE})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+2026)"
        r"\s*[–-]\s*"
        rf"((?:{MONTH_RE})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+20\d{{2}}|Ongoing|Temporarily Unavailable)",
        text,
        re.I,
    )
    if exact_range:
        return None, None, normalize_space(exact_range.group(0))
    exact = re.search(rf"((?:{MONTH_RE})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+2026)", text, re.I)
    if exact:
        return None, None, normalize_space(exact.group(1))
    season = re.search(r"\b(Spring|Summer|Fall|Autumn|Winter)\s+2026\b", text, re.I)
    if season:
        return None, None, normalize_space(season.group(0))
    year = re.search(r"\b2026\b", text)
    if year:
        return None, None, "2026"
    return None, None, None


def label_precision(label: str | None) -> str:
    if not label:
        return "unknown"
    if re.search(r"\b(Spring|Summer|Fall|Autumn|Winter)\b", label, re.I):
        return "season"
    if re.fullmatch(r"2026", label):
        return "year"
    if re.search(r"\d{1,2}", label):
        return "exact_or_range"
    if "ongoing" in label.lower():
        return "ongoing"
    return "label"


def parse_month(value: str) -> int:
    return MONTH_NUMBERS[value.strip(".").lower()]


def parse_us_date(value: str) -> dt.date | None:
    match = re.fullmatch(rf"({MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(20\d{{2}})", normalize_space(value), re.I)
    if not match:
        return None
    try:
        return dt.date(int(match.group(3)), parse_month(match.group(1)), int(match.group(2)))
    except ValueError:
        return None


def parse_label_start_date(label: str | None) -> dt.date | None:
    if not label:
        return None
    normalized = normalize_space(label)
    if re.match(r"\bthrough\b", normalized, re.I) or re.fullmatch(r"ongoing", normalized, re.I):
        return None
    explicit = re.match(rf"({MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(20\d{{2}})", normalized, re.I)
    if explicit:
        return parse_us_date(explicit.group(0))
    month_year = re.match(rf"({MONTH_RE})\.?\s+(20\d{{2}})\b", normalized, re.I)
    if month_year:
        return dt.date(int(month_year.group(2)), parse_month(month_year.group(1)), 1)
    year_match = re.search(r"\b(20\d{2})\b", normalized)
    month_day = re.match(rf"({MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?!\d)", normalized, re.I)
    if year_match and month_day:
        try:
            return dt.date(int(year_match.group(1)), parse_month(month_day.group(1)), int(month_day.group(2)))
        except ValueError:
            return None
    return None


def date_to_iso(value: dt.date | None) -> str | None:
    return value.isoformat() if value else None

def source_item(title: str, url: str, text: str = "", **extra: Any) -> dict[str, Any]:
    _, _, date_label = detect_date_label(" ".join([title, text]))
    date_start = date_to_iso(parse_label_start_date(date_label))
    item = {
        "title": normalize_space(title),
        "date_start": date_start,
        "date_label": date_label,
        "date_precision": label_precision(date_label),
        "source_url": url,
        "external_id": url,
        **extra,
    }
    return item


CARRIED_OVER_BROADWAY_TITLES = {
    "& juliet",
    "aladdin",
    "the book of mormon",
    "buena vista social club",
    "chicago",
    "death becomes her",
    "the great gatsby",
    "hadestown",
    "hamilton",
    "harry potter and the cursed child",
    "just in time",
    "the lion king",
    "maybe happy ending",
    "mj",
    "moulin rouge! the musical",
    "oh, mary!",
    "operation mincemeat",
    "the outsiders",
    "six: the musical",
    "stranger things: the first shadow",
    "titaníque",
    "two strangers (carry a cake across new york)",
    "wicked",
}


# Open-ended Off-Broadway runs that are perennially "still playing," not new productions.
OFF_BROADWAY_OPEN_RUNS = {
    "blue man group",
    "drunk shakespeare",
    "perfect crime",
    "the imbible: a spirited history of drinking",
    "the office! a musical parody",
    "friends! the musical parody",
    "the play that goes wrong",
    "stomp",
    "sleep no more",
}


def is_carried_over_title(title: str, extra: set[str] | None = None) -> bool:
    normalized = normalize_space(title).lower()
    base_title = re.sub(r"\s*:\s*(?:a new musical|the musical|.*)$", "", normalized)
    known = CARRIED_OVER_BROADWAY_TITLES | (extra or set())
    return normalized in known or base_title in known


def is_carried_over_broadway_title(title: str) -> bool:
    return is_carried_over_title(title)


def production_year_from_url(url: str) -> int | None:
    years = [int(year) for year in re.findall(r"(?:^|[-/])(20\d{2})(?:[-/]|$)", url)]
    return max(years) if years else None


def clean_sponsor_prefix(title: str) -> str:
    title = normalize_space(title)
    prefixes = [
        r"Hyundai Card FirstLook:\s*",
        r"Modern Mural:\s*",
        r"The Modern Window:\s*",
        r"Artist[’']s Choice:\s*",
    ]
    for prefix in prefixes:
        title = re.sub(rf"^{prefix}", "", title, flags=re.I)
    return normalize_space(title)


def clean_moma_title(title: str) -> str:
    title = clean_sponsor_prefix(title)
    title = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", title)
    title = re.sub(r"(?<=[a-z])and\s+(?=[A-Z])", " and ", title)
    title = re.sub(r"\s*:\s*", ": ", title)
    words = title.split()
    if len(words) % 2 == 0 and words[: len(words) // 2] == words[len(words) // 2 :]:
        title = " ".join(words[: len(words) // 2])
    return normalize_space(title)


def is_future_moma_item(date_label: str | None) -> bool:
    start = parse_label_start_date(date_label)
    return bool(start and today() <= start <= end_date())


def extract_moma_title_date(text: str) -> tuple[str, str | None]:
    text = normalize_space(text)
    patterns = [
        rf"(?P<label>(?:{MONTH_PATTERN})\.?\s+\d{{1,2}},?\s+2025\s*[–-]\s*ongoing)",
        rf"(?P<label>(?:{MONTH_PATTERN})\.?\s+\d{{1,2}},?\s+2027\s*[–-]\s*(?:{MONTH_PATTERN})\.?\s+\d{{1,2}},?\s+2027)",
        rf"(?P<label>(?:{MONTH_PATTERN})\.?\s+\d{{1,2}}\s*[–-]\s*(?:{MONTH_PATTERN})\.?\s+\d{{1,2}},?\s+2027)",
        rf"(?P<label>(?:{MONTH_PATTERN})\.?\s+\d{{1,2}},?\s+2026\s*[–-]\s*(?:{MONTH_PATTERN})\.?\s+\d{{1,2}},?\s+2027)",
        rf"(?P<label>(?:{MONTH_PATTERN})\.?\s+\d{{1,2}},?\s+2026\s*[–-]\s*(?:Spring|Summer|Fall|Winter)\s+2027)",
        rf"(?P<label>(?:{MONTH_PATTERN})\.?\s+\d{{1,2}},?\s+2026\s*[–-]\s*(?:Spring|Summer|Fall|Winter)\s+2026)",
        rf"(?P<label>(?:{MONTH_PATTERN})\.?\s+\d{{1,2}}\s*[–-]\s*(?:{MONTH_PATTERN})\.?\s+\d{{1,2}},?\s+2026)",
        rf"(?P<label>(?:{MONTH_PATTERN})\.?\s+\d{{1,2}},?\s+2026)",
        rf"(?P<label>(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+2026)",
        r"(?P<label>Through\s+(?:Spring|Summer|Fall|Winter)\s+2027)",
        rf"(?P<label>Through\s+(?:{MONTH_PATTERN})\.?\s+\d{{1,2}})",
        r"(?P<label>Ongoing)",
    ]
    label = None
    match = None
    for pattern in patterns:
        matches = list(re.finditer(pattern, text, re.I))
        if matches:
            match = matches[-1]
            label = normalize_space(match.group("label"))
            break
    if not label:
        return text, None
    title = normalize_space(text[: match.start()])
    title = re.sub(rf"\s*Member Previews,?\s+(?:{MONTH_PATTERN})\.?\s+\d{{1,2}}\s*[–-]\s*\d{{1,2}}\s*$", "", title, flags=re.I).strip()
    title = re.sub(r"\bMember Previews,?\s*$", "", title).strip()
    title = re.sub(r"\bLast chance\s*$", "", title).strip()
    return clean_moma_title(title or text), label


def moma_live_enabled() -> bool:
    """The MoMA live scraper is opt-in (default off): moma.org's WAF 403s our CI and dev
    hosts, so the committed fixture is the source of truth until a verified capture exists.
    Set MOMA_LIVE=1 on a non-blocked editorial/network host to let the live path run."""
    return os.environ.get("MOMA_LIVE", "").strip().lower() in {"1", "true", "yes", "on"}


def moma_upcoming_section(text: str) -> str | None:
    """Slice the index HTML to the 'Upcoming exhibitions' section, ending before the boundary
    heading 'Installations and projects'. Returns None when either heading is missing — the
    tell that we got a 403 challenge or JS shell rather than the real server-rendered index."""
    start = re.search(r"Upcoming exhibitions", text, re.I)
    end = re.search(r"Installations and projects", text, re.I)
    if not start or not end or end.start() <= start.start():
        return None
    return text[start.start():end.start()]


def moma_document_valid(text: str) -> bool:
    """A fetched MoMA index is trustworthy only if it has both section headings AND at least
    one /calendar/exhibitions/<id> link inside the Upcoming section. This is the gate that lets
    the live path override the fixture; anything else leaves the fixture untouched."""
    section = moma_upcoming_section(text or "")
    return bool(section and re.search(r"/calendar/exhibitions/\d+", section))


def parse_moma_capture(source: Source, limit: int = 80) -> list[dict[str, Any]]:
    if not MOMA_CAPTURE_LINKS.exists():
        return []
    data = json.loads(MOMA_CAPTURE_LINKS.read_text())
    if isinstance(data, dict) and "items" in data:
        # Structured fixture refreshed from a verified live parse — keep only in-horizon rows.
        kept = [dict(item) for item in data["items"] if is_future_moma_item(item.get("date_label"))]
        return kept[:limit]
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in data.get("exhibitionLinks", []):
        url = link.get("href")
        raw_text = link.get("text") or ""
        if not url or url in seen:
            continue
        seen.add(url)
        title, date_label = extract_moma_title_date(raw_text)
        if not is_future_moma_item(date_label):
            continue
        date_start = date_to_iso(parse_label_start_date(date_label))
        items.append(
            {
                "title": title,
                "date_start": date_start,
                "date_label": date_label,
                "date_precision": label_precision(date_label),
                "venue_or_platform": "MoMA",
                "city": "New York",
                "source_url": url,
                "external_id": url,
                "description": "browser capture fallback",
            }
        )
        if len(items) >= limit:
            break
    return items


def parse_moma_exhibitions(source: Source, text: str, limit: int = 100) -> list[dict[str, Any]]:
    # Parse only the Upcoming section (between its heading and 'Installations and projects'),
    # so current/ongoing shows below the boundary never leak in. A missing section means the
    # fetch wasn't the real index — return nothing and let the caller serve the fixture.
    section = moma_upcoming_section(text)
    if section is None:
        return []
    parser = LinkTextParser(source.url)
    parser.feed(section)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in parser.links:
        if not re.search(r"https://www\.moma\.org/calendar/exhibitions/\d+$", link["url"]):
            continue
        if link["url"] in seen:
            continue
        seen.add(link["url"])
        title, date_label = extract_moma_title_date(link["text"])
        if not is_future_moma_item(date_label):
            continue
        date_start = date_to_iso(parse_label_start_date(date_label))
        items.append(
            {
                "title": title,
                "date_start": date_start,
                "date_label": date_label,
                "date_precision": label_precision(date_label),
                "venue_or_platform": "MoMA",
                "city": "New York",
                "source_url": link["url"],
                "external_id": link["url"],
            }
        )
        if len(items) >= limit:
            break
    return items


def parse_broadway_org(source: Source, text: str, limit: int = 100) -> list[dict[str, Any]]:
    parser = LinkTextParser(source.url)
    parser.feed(text)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in parser.links:
        url = link["url"]
        title = link["text"]
        if not re.fullmatch(r"https://www\.broadway\.org/shows/[^/?#]+", url):
            continue
        if url in seen or title.lower() in {"english", "shows"}:
            continue
        if is_carried_over_broadway_title(title):
            continue
        seen.add(url)
        items.append(source_item(title, url, venue_or_platform="Broadway"))
        if len(items) >= limit:
            break
    return items


def extract_broadway_date_fields(html_text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    field_pattern = re.compile(
        rf"(First Preview Date|Opening Date|Closing Date):(?:&nbsp;|\s|<[^>]+>)*"
        rf"({MONTH_RE}\.?\s+\d{{1,2}},\s+20\d{{2}})",
        re.I,
    )
    for label, value in field_pattern.findall(html.unescape(html_text)):
        normalized_label = normalize_space(label).lower()
        key = {
            "first preview date": "first_preview_date",
            "opening date": "opening_date",
            "closing date": "closing_date",
        }.get(normalized_label)
        if key:
            fields[key] = normalize_space(value)
    if fields:
        return fields

    text = strip_tags(html_text)
    fallback_pattern = re.compile(
        rf"(First Preview Date|Opening Date|Closing Date):\s*"
        rf"({MONTH_RE}\.?\s+\d{{1,2}},\s+20\d{{2}})",
        re.I,
    )
    for label, value in fallback_pattern.findall(text):
        normalized_label = normalize_space(label).lower()
        key = {
            "first preview date": "first_preview_date",
            "opening date": "opening_date",
            "closing date": "closing_date",
        }.get(normalized_label)
        if key:
            fields[key] = normalize_space(value)
    return fields


def apply_broadway_date_fields(item: dict[str, Any], fields: dict[str, str]) -> None:
    opening_label = fields.get("opening_date")
    opening_date = parse_us_date(opening_label) if opening_label else None
    if opening_date:
        item["date_start"] = opening_date.isoformat()
        item["date_label"] = opening_label
        item["date_precision"] = "exact"
        item["importance_score"] = max(item.get("importance_score") or 0, 20)

    notes = []
    if fields.get("first_preview_date"):
        notes.append(f"First preview: {fields['first_preview_date']}")
    if fields.get("opening_date"):
        notes.append(f"Opening: {fields['opening_date']}")
    if fields.get("closing_date"):
        notes.append(f"Closing: {fields['closing_date']}")
        closing_date = parse_us_date(fields["closing_date"])
        if closing_date:
            item["date_end"] = closing_date.isoformat()
    if notes:
        item["description"] = "; ".join(notes)


def parse_playbill(source: Source, text: str, limit: int = 120) -> list[dict[str, Any]]:
    parser = ArticleParser(source.url)
    parser.feed(text)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    bad_titles = {"Trending", "View Details", "Buy Tickets", "Image", "2026 Tony Winner"}
    for article in parser.articles:
        production_links = [
            link for link in article["links"]
            if "playbill.com/production/" in link["url"] and link["text"] not in bad_titles
        ]
        if not production_links:
            continue
        link = max(production_links, key=lambda candidate: len(candidate["text"]))
        if link["url"] in seen:
            continue
        year = production_year_from_url(link["url"])
        if is_carried_over_broadway_title(link["text"]):
            continue
        if year and year < 2025 and not re.search(r"\b2026\b", article["text"]):
            continue
        if re.search(r"\bCloses?\s+", article["text"], re.I):
            continue
        seen.add(link["url"])
        tag = " ".join(sorted({l["text"] for l in article["links"] if l["text"] in {"Trending", "2026 Tony Winner"}}))
        item = source_item(
            link["text"],
            link["url"],
            article["text"],
            venue_or_platform="Broadway",
            description=tag or None,
        )
        if not item.get("date_start"):
            continue
        items.append(item)
        if len(items) >= limit:
            break
    return items


def extract_offbroadway_open_date(card_text: str) -> tuple[str | None, str | None]:
    """Pull an opening/preview date from a Playbill card, never a closing date.

    Off-Broadway cards read "<Title> <signal> <date> <venue>", where the signal is
    "Opening Night"/"Opens" (a new production) or "Begins Previews" (arriving), or
    "Closes" (a show that is merely still running). We only honor opening/preview
    signals so a closing date can never masquerade as a planning date.
    """
    date = rf"({MONTH_RE}\.?\s+\d{{1,2}},?\s+20\d{{2}})"
    for kind, verb in (("Opening", r"Opening Night"), ("Opens", r"Opens"), ("Opens", r"Opening"), ("Previews", r"Begins Previews")):
        match = re.search(rf"{verb}\s+{date}", card_text, re.I)
        if match:
            return normalize_space(match.group(1)), kind
    return None, None


def parse_playbill_offbroadway(source: Source, text: str, limit: int = 120) -> list[dict[str, Any]]:
    parser = ArticleParser(source.url)
    parser.feed(text)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    bad_titles = {"Trending", "View Details", "Buy Tickets", "Image", ""}
    for article in parser.articles:
        production_links = [
            link for link in article["links"]
            if "playbill.com/production/" in link["url"] and link["text"] not in bad_titles
        ]
        if not production_links:
            continue
        link = max(production_links, key=lambda candidate: len(candidate["text"]))
        if link["url"] in seen:
            continue
        title = link["text"]
        if is_carried_over_title(title, OFF_BROADWAY_OPEN_RUNS):
            continue
        date_label, kind = extract_offbroadway_open_date(article["text"])
        if not date_label:
            # Closes-only or undated card: a show still running, not a new production.
            continue
        start = parse_us_date(date_label)
        if not start or start < today() or start > end_date():
            continue
        seen.add(link["url"])
        venue_match = re.search(rf"{re.escape(date_label)}\s+(.*?)\s+View Details", article["text"])
        venue = normalize_space(venue_match.group(1)) if venue_match else ""
        verb_label = "Begins previews" if kind == "Previews" else "Opens"
        items.append(
            {
                "title": normalize_space(title),
                "date_start": start.isoformat(),
                "date_label": date_label,
                "date_precision": "exact",
                "venue_or_platform": venue or "Off-Broadway",
                "city": "New York",
                "source_url": link["url"],
                "external_id": link["url"],
                "description": f"Off-Broadway. {verb_label}: {date_label}",
                "importance_score": 12,
            }
        )
        if len(items) >= limit:
            break
    return items


# BAM mixes multiple disciplines in one feed; map its genre tag to our categories.
BAM_GENRE_CATEGORY = {
    "music": "music",
    "dance": "ballet",
    "opera": "opera",
    "film": "film",
}
# Flagship BAM season umbrellas that are genuine future planning signals even when undated.
BAM_FUTURE_FESTIVALS = {"next wave"}


def format_us_date(value: dt.date) -> str:
    return f"{value.strftime('%b')} {value.day}, {value.year}"


def parse_bam(source: Source, text: str, limit: int = 80) -> list[dict[str, Any]]:
    """BAM's programs page exposes machine-readable productionblocks.

    Each carries data-sort-title / data-sort-date / data-sort-genre. We keep discrete
    future-dated events and, for BAM's flagship fall festival (Next Wave), a season
    planning row even when the umbrella entry itself is undated. Year-long umbrellas
    ("BAM Film 2026", "The Met: Live in HD"), past events, and current/half-past
    seasons are dropped.
    """
    blocks = re.findall(
        r'<div class="productionblock"[^>]*?data-sort-title="([^"]*)"[^>]*?'
        r'data-sort-date="([^"]*)"[^>]*?data-sort-genre="([^"]*)"',
        text,
    )
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_title, raw_date, raw_genre in blocks:
        title = normalize_space(html.unescape(raw_title))
        genre = normalize_space(html.unescape(raw_genre)).lower()
        if not title or title.lower() in seen:
            continue
        category = BAM_GENRE_CATEGORY.get(genre, "theatre")
        external_id = f"bam:{title.lower()}"
        iso = raw_date[:10]
        start = None
        if re.fullmatch(r"20\d\d-\d\d-\d\d", iso):
            try:
                start = dt.date.fromisoformat(iso)
            except ValueError:
                start = None
        if start and today() <= start <= end_date():
            seen.add(title.lower())
            items.append(
                {
                    "title": title,
                    "category": category,
                    "date_start": start.isoformat(),
                    "date_label": format_us_date(start),
                    "date_precision": "exact",
                    "venue_or_platform": "BAM",
                    "city": "Brooklyn",
                    "source_url": "https://www.bam.org/programs",
                    "external_id": external_id,
                    "description": f"BAM {genre or 'program'}",
                    "importance_score": 14,
                }
            )
        elif not start:
            base = re.sub(r"\s*20\d\d.*$", "", title).strip().lower()
            if base in BAM_FUTURE_FESTIVALS and "2026" in title:
                seen.add(title.lower())
                items.append(
                    {
                        "title": title,
                        "category": category,
                        "date_label": "Fall 2026",
                        "date_precision": "season",
                        "venue_or_platform": "BAM",
                        "city": "Brooklyn",
                        "source_url": "https://www.bam.org/programs",
                        "external_id": external_id,
                        "description": "BAM festival season",
                        "importance_score": 18,
                    }
                )
        if len(items) >= limit:
            break
    return items


# Curated prominence gate for albums. Neither AOTY's upcoming feed nor MusicBrainz ranks
# by prominence, so an editorial allowlist is the only way to surface "major" drops rather
# than a firehose of every release. Extend as coverage priorities shift.
NOTABLE_MUSIC_ARTISTS = {
    "adele", "arca", "ariana grande", "bad bunny", "beyoncé", "beyonce", "billie eilish",
    "blood orange", "bon iver", "burna boy", "cardi b", "charli xcx", "clipse",
    "doechii", "doja cat", "drake", "dua lipa", "fka twigs", "florence + the machine",
    "frank ocean", "fred again..", "future", "gracie abrams", "j. cole", "jack white",
    "jamie xx", "janelle monáe", "janelle monae", "jpegmafia", "kacey musgraves",
    "kendrick lamar", "kim gordon", "king gizzard & the lizard wizard", "lady gaga",
    "lana del rey", "lcd soundsystem", "lil nas x", "lorde", "mdou moctar", "megan thee stallion",
    "mitski", "nas", "nick cave & the bad seeds", "olivia rodrigo", "perfume genius",
    "phoebe bridgers", "playboi carti", "post malone", "pusha t", "rosalía", "rosalia",
    "sabrina carpenter", "sault", "sza", "st. vincent", "taylor swift", "the cure",
    "the national", "the weeknd", "tyler, the creator", "vampire weekend", "vince staples",
    "weyes blood", "wilco", "wizkid", "yo la tengo",
}


def normalize_artist_name(name: str) -> str:
    name = normalize_space(html.unescape(name)).lower()
    return re.sub(r"\s+(?:feat\.?|featuring|with|x|&|,).*$", "", name).strip()


def is_notable_artist(name: str) -> bool:
    return normalize_artist_name(name) in NOTABLE_MUSIC_ARTISTS


def parse_aoty_date(label: str, ref: dt.date | None = None) -> dt.date | None:
    """AlbumOfTheYear's upcoming list omits the year ("Jun 13"); infer it from context.

    The list is forward-looking from today, so a month earlier than the current month
    belongs to next year.
    """
    ref = ref or today()
    match = re.fullmatch(rf"({MONTH_RE})\.?\s+(\d{{1,2}})", normalize_space(label), re.I)
    if not match:
        return None
    month = parse_month(match.group(1))
    year = ref.year if month >= ref.month else ref.year + 1
    try:
        return dt.date(year, month, int(match.group(2)))
    except ValueError:
        return None


def parse_aoty_upcoming(source: Source, text: str, limit: int = 40) -> list[dict[str, Any]]:
    """Upcoming album releases from AlbumOfTheYear.

    Note: AOTY's upcoming feed is a near-term firehose with no prominence ranking, so
    this keeps full-length LPs only (dropping EP/mixtape/single/remix/live) and caps the
    list. Editorial prominence filtering is the open gap for this lane (Metacritic's
    anticipated list is blocked); see handover.
    """
    blocks = re.split(r'<div class="albumBlock', text)[1:]
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for block in blocks:
        href = re.search(r'href="(/album/[^"]+\.php)"', block)
        artist = re.search(r'<div class="artistTitle">(.*?)</div>', block, re.S)
        album = re.search(r'<div class="albumTitle">(.*?)</div>', block, re.S)
        type_div = re.search(r'<div class="type">(.*?)</div>', block, re.S)
        if not (href and artist and album and type_div):
            continue
        url = urljoin(source.url, href.group(1))
        if url in seen:
            continue
        type_text = normalize_space(html.unescape(type_div.group(1)))
        date_format = re.match(r"(.*?)\s*[•·|]\s*(\w+)", type_text)
        if not date_format:
            continue
        fmt = date_format.group(2)
        if fmt.lower() != "lp":
            continue
        if not is_notable_artist(artist.group(1)):
            continue
        start = parse_aoty_date(date_format.group(1))
        if not start or start < today() or start > end_date():
            continue
        seen.add(url)
        artist_name = normalize_space(html.unescape(artist.group(1)))
        album_name = normalize_space(html.unescape(album.group(1)))
        items.append(
            {
                "title": f"{artist_name} — {album_name}",
                "date_start": start.isoformat(),
                "date_label": format_us_date(start),
                "date_precision": "exact",
                "venue_or_platform": artist_name,
                "source_url": url,
                "external_id": url,
                "people": [{"name": artist_name, "role": "Artist"}],
                "description": "Album release (LP)",
                "importance_score": 8,
            }
        )
        if len(items) >= limit:
            break
    return items


def parse_metacritic_date(label: str) -> tuple[str | None, str | None, str | None]:
    """Parse Metacritic release-calendar date labels into (date_start, date_label, precision).

    Handles firm dates ("12 June 2026"), month windows ("Dec 2026"), bare years ("2026"),
    and the anticipated section's "TBA". Returns (None, None, None) for non-dates such as
    the "Anticipated Future Releases" section header.
    """
    label = normalize_space(label)
    firm = re.fullmatch(rf"(\d{{1,2}})\s+({MONTH_RE})\.?\s+(20\d{{2}})", label, re.I)
    if firm:
        try:
            day = dt.date(int(firm.group(3)), parse_month(firm.group(2)), int(firm.group(1)))
            return day.isoformat(), format_us_date(day), "exact"
        except ValueError:
            return None, None, None
    month_year = re.fullmatch(rf"({MONTH_RE})\.?\s+(20\d{{2}})", label, re.I)
    if month_year:
        first = dt.date(int(month_year.group(2)), parse_month(month_year.group(1)), 1)
        return first.isoformat(), label, "month"
    if re.fullmatch(r"20\d{2}", label):
        return None, label, "year"
    if re.fullmatch(r"TBA", label, re.I):
        return None, "TBA", "year"
    return None, None, None


def parse_metacritic_albums(source: Source, text: str, limit: int = 220) -> list[dict[str, Any]]:
    """Metacritic's Upcoming Album Release Calendar — firm dates plus an anticipated
    (vague-date) section. Notable artists are boosted so they sort to the top without
    discarding the curated long tail; vague rows are kept as planning signals.
    """
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for table in re.findall(r'<table class="musicTable".*?</table>', text, re.S):
        header_info: tuple[str | None, str | None, str | None] = (None, None, None)
        for row in re.findall(r"<tr[^>]*>.*?</tr>", table, re.S):
            head = re.search(r"<th[^>]*>(.*?)</th>", row, re.S)
            if head:
                header_info = parse_metacritic_date(strip_tags(head.group(1)))
                continue
            artist_cell = re.search(r'<td class="artistName">(.*?)</td>', row, re.S)
            album_cell = re.search(r'<td class="albumTitle">(.*?)</td>', row, re.S)
            if not (artist_cell and album_cell):
                continue
            comment_cell = re.search(r'<td class="dataComment">(.*?)</td>', row, re.S)
            artist = strip_tags(artist_cell.group(1))
            album = strip_tags(album_cell.group(1))
            comment = strip_tags(comment_cell.group(1)) if comment_cell else ""
            if not artist or not album:
                continue
            date_start, date_label, precision = header_info
            note = ""
            if precision is None:
                date_start, date_label, precision = parse_metacritic_date(comment)
            else:
                note = comment
            if precision is None:
                continue
            if date_start:
                day = dt.date.fromisoformat(date_start)
                if day < today() or day > end_date():
                    continue
            elif not re.search(r"2026|TBA", date_label or "", re.I):
                continue
            key = (artist.lower(), album.lower())
            if key in seen:
                continue
            seen.add(key)
            description = "Album release" + (f". {note}" if note else "")
            items.append(
                {
                    "title": f"{artist} — {album}",
                    "date_start": date_start,
                    "date_label": date_label,
                    "date_precision": precision,
                    "venue_or_platform": "",  # artist already in the title; avoid redundancy
                    "people": [{"name": artist, "role": "Artist"}],
                    "source_url": source.url,
                    "external_id": f"mc:{artist.lower()}|{album.lower()}",
                    "description": description,
                    "importance_score": 25 if is_notable_artist(artist) else 10,
                }
            )
            if len(items) >= limit:
                return items
    return items

def parse_frick_capture(source: Source) -> list[dict[str, Any]]:
    """Parse the browser-captured Frick fixture (Drupal site behind Yottaa anti-bot; see
    the fixture note). Future-opening exhibitions within the horizon only."""
    if not FRICK_CAPTURE.exists():
        return []
    data = json.loads(FRICK_CAPTURE.read_text())
    items: list[dict[str, Any]] = []
    for ex in data.get("exhibitions", []):
        try:
            start = dt.date.fromisoformat(ex["start"])
        except (ValueError, KeyError):
            continue
        if start < today() or start > end_date():
            continue
        title = normalize_space(ex.get("title", ""))
        if not title:
            continue
        items.append(
            {
                "title": title,
                "category": "art",
                "date_start": start.isoformat(),
                "date_label": normalize_space(ex.get("label", "")) or format_us_date(start),
                "date_precision": "exact",
                "venue_or_platform": "Frick Collection",
                "city": "New York",
                "source_url": "https://www.frick.org/exhibitions",
                "external_id": f"frick:{title.lower()[:50]}",
                "description": "Frick Collection, New York",
            }
        )
    return items


# Ocula (ocula.com) aggregates gallery shows as clean rows but Cloudflare-walls scripts
# (curl/requests 403; only a real browser fetches it), so it's a browser-capture source —
# refresh the fixture via Claude-in-Chrome (see capture/README.md). The allowlist deliberately
# EXCLUDES galleries we already scrape directly (Gagosian, Pace), so Ocula only adds the
# majors we otherwise can't (White Cube, Zwirner, Hauser & Wirth, Lehmann Maupin, Gladstone…)
# and there's no cross-source art dedupe to maintain. Key is Ocula's gallery slug.
OCULA_MAJOR_GALLERIES = {
    "david-zwirner": "David Zwirner", "hauser-wirth": "Hauser & Wirth", "white-cube": "White Cube",
    "lehmann-maupin": "Lehmann Maupin", "gladstone-gallery": "Gladstone",
    "matthew-marks-gallery": "Matthew Marks",  # Marian Goodman scraped directly (import_marian_goodman)
    "paula-cooper-gallery": "Paula Cooper", "303-gallery": "303 Gallery", "petzel": "Petzel",
    "sean-kelly": "Sean Kelly", "lisson-gallery": "Lisson Gallery", "sprueth-magers": "Sprüth Magers",
    "kasmin-gallery": "Kasmin", "luhring-augustine": "Luhring Augustine", "casey-kaplan": "Casey Kaplan",
    "andrew-kreps-gallery": "Andrew Kreps", "tanya-bonakdar-gallery": "Tanya Bonakdar",
}


def _ocula_slug(href: str) -> str | None:
    m = re.search(r"/(?:art-galleries|exhibition-previews)/([^/]+)/", href or "")
    return m.group(1) if m else None


def parse_ocula_dates(text: str):
    """Parse Ocula's date strings → (start_iso, end_iso|None, label, precision) or None.
    Handles 'From 4 March 2026', '11 June–14 August 2026' (year at end), and
    '12 November 2026–23 January 2027' (cross-year)."""
    M = MONTH_PATTERN

    def mk(year, month, day):
        try:
            return dt.date(int(year), MONTH_NUMBERS[str(month).strip(".").lower()], int(day))
        except (KeyError, ValueError):
            return None

    m = re.search(rf"From\s+(\d{{1,2}})\s+({M})\.?\s+(\d{{4}})", text)
    if m:
        d = mk(m.group(3), m.group(2), m.group(1))
        return (d.isoformat(), None, f"From {format_us_date(d)}", "exact") if d else None

    m = re.search(rf"(\d{{1,2}})\s+({M})\.?(?:\s+(\d{{4}}))?\s*[–-]\s*(\d{{1,2}})\s+({M})\.?\s+(\d{{4}})", text)
    if m:
        sd, sm, sy, ed, em, ey = m.groups()
        end = mk(ey, em, ed)
        start_year = int(sy) if sy else int(ey)
        if not sy and MONTH_NUMBERS.get(sm.lower(), 0) > MONTH_NUMBERS.get(em.lower(), 0):
            start_year -= 1  # range wraps a year boundary, e.g. "Dec–Jan 2027"
        start = mk(start_year, sm, sd)
        if not start:
            return None
        label = format_us_date(start) + (f" – {format_us_date(end)}" if end else "")
        return (start.isoformat(), end.isoformat() if end else None, label, "exact")

    m = re.search(rf"(\d{{1,2}})\s+({M})\.?\s+(\d{{4}})", text)
    if m:
        d = mk(m.group(3), m.group(2), m.group(1))
        return (d.isoformat(), None, format_us_date(d), "exact") if d else None
    return None


def parse_ocula_exhibitions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Major-gallery, New York, future-opening exhibition rows from the Ocula capture."""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        slug = row.get("gallery_slug") or _ocula_slug(row.get("href", ""))
        gallery = OCULA_MAJOR_GALLERIES.get(slug)
        if not gallery:
            continue
        text = normalize_space(html.unescape(row.get("text", "")))
        if "new york" not in text.lower():
            continue
        parsed = parse_ocula_dates(text)
        if not parsed:
            continue
        start_iso, end_iso, label, precision = parsed
        start = dt.date.fromisoformat(start_iso)
        if start < today() or start > end_date():
            continue
        href = row.get("href", "")
        ext = f"ocula:{href}"
        if ext in seen:
            continue
        seen.add(ext)
        title = normalize_space(text.split(gallery)[0]) if gallery in text else text
        items.append({
            "title": title or gallery,
            "category": "art",
            "date_start": start_iso,
            "date_end": end_iso,
            "date_label": label,
            "date_precision": precision,
            "venue_or_platform": gallery,
            "city": "New York",
            "source_url": "https://ocula.com" + href,
            "external_id": ext,
            "description": f"{gallery}, New York",
            "importance_score": 14,
        })
    return items


def parse_ocula_fairs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Major NY art fairs from the Ocula capture (future-only). All notable fairs Ocula
    lists are kept — its fair list is already curated."""
    items: list[dict[str, Any]] = []
    for row in rows:
        text = normalize_space(html.unescape(row.get("text", "")))
        if "new york" not in text.lower():
            continue
        parsed = parse_ocula_dates(text)
        if not parsed:
            continue
        start_iso, end_iso, label, precision = parsed
        start = dt.date.fromisoformat(start_iso)
        if start < today() or start > end_date():
            continue
        name = re.split(rf"\s+\d{{1,2}}\s*[–-]?\s*\d{{0,2}}\s*(?:{MONTH_PATTERN})", text, maxsplit=1)[0]
        name = normalize_space(re.sub(r"\s+20\d{2}$", "", name)) or "Art Fair"
        items.append({
            "title": name,
            "category": "art",
            "date_start": start_iso,
            "date_end": end_iso,
            "date_label": label,
            "date_precision": precision,
            "venue_or_platform": "Art fair",
            "city": "New York",
            "source_url": "https://ocula.com/art-fairs/" + (row.get("slug", "")),
            "external_id": f"ocula-fair:{row.get('slug','')}",
            "description": "New York art fair",
            "importance_score": 18,
        })
    return items


def import_ocula(conn: sqlite3.Connection, source: Source) -> int:
    """Major NY gallery shows + fairs from the Ocula browser-capture fixture."""
    if not OCULA_CAPTURE.exists():
        record_run(conn, source, "skipped", "no Ocula fixture")
        return 0
    data = json.loads(OCULA_CAPTURE.read_text())
    items = parse_ocula_exhibitions(data.get("exhibitions", [])) + parse_ocula_fairs(data.get("fairs", []))
    for item in items:
        upsert_item(conn, source, item)
        ensure_model_enrichment_placeholder(conn, source, item)
    record_run(conn, source, "ok", f"parsed {len(items)} NY gallery shows/fairs from Ocula capture")
    return len(items)


def parse_marian_goodman(source: Source, text: str) -> list[dict[str, Any]]:
    """Marian Goodman's own /exhibitions/ listing (server-rendered, scriptable — Ocula doesn't
    surface its forthcoming NY shows). NY-only (gallery rule), future-opening only. Each card:
    a `class="area"` block with location + heading_title + subheading, followed by a
    `class="bottom"` div holding the date range."""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chunk in text.split('class="area"')[1:]:
        href = re.search(r'<a href="([^"]+)"', chunk)
        location = re.search(r'<span class="location">([^<]+)</span>', chunk)
        artist = re.search(r'<span class="heading_title">([^<]*)</span>', chunk)
        date = re.search(r'<div class="bottom[^"]*">\s*([^<]+?)\s*</div>', chunk)
        if not (href and location and artist and date):
            continue
        if "new york" not in location.group(1).lower():
            continue
        parsed = parse_ocula_dates(normalize_space(html.unescape(date.group(1))))
        if not parsed:
            continue
        start_iso, end_iso, label, precision = parsed
        start = dt.date.fromisoformat(start_iso)
        if start < today() or start > end_date():
            continue
        name = normalize_space(html.unescape(artist.group(1)))
        sub = re.search(r'<div class="subheading[^"]*">([^<]*)</div>', chunk)
        subtitle = normalize_space(html.unescape(sub.group(1))) if sub else ""
        if not name:
            continue
        path = href.group(1)
        ext = f"mariangoodman:{path}"
        if ext in seen:
            continue
        seen.add(ext)
        items.append({
            "title": f"{name}: {subtitle}" if subtitle else name,
            "category": "art",
            "date_start": start_iso,
            "date_end": end_iso,
            "date_label": label,
            "date_precision": precision,
            "venue_or_platform": "Marian Goodman",
            "city": "New York",
            "source_url": "https://www.mariangoodman.com" + path,
            "external_id": ext,
            "description": "Marian Goodman Gallery, New York",
            "importance_score": 14,
        })
    return items


def import_marian_goodman(conn: sqlite3.Connection, source: Source) -> int:
    """Marian Goodman NY forthcoming exhibitions — scriptable, cache-backed per the integrity rule."""
    return import_with_cache(conn, source, MARIAN_GOODMAN_CACHE, parse_marian_goodman,
                             must_contain=("heading_title",))


def parse_met_exhibitions(source: Source, text: str, limit: int = 120) -> list[dict[str, Any]]:
    upcoming_match = re.search(r'<section[^>]+id="upcoming"[^>]*>', text, re.I)
    if upcoming_match:
        text = text[upcoming_match.start():]
    parser = ArticleParser(source.url)
    parser.feed(text)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for article in parser.articles:
        exhibition_links = [
            link for link in article["links"]
            if re.search(r"https://www\.metmuseum\.org/(?:en/)?exhibitions/[^/?#]+$", link["url"])
            and not re.search(r"/exhibitions/(?:past|past-exhibitions)(?:/|$)", link["url"])
        ]
        if not exhibition_links:
            continue
        link = max(exhibition_links, key=lambda candidate: len(candidate["text"]))
        title = link["text"] or article["text"]
        if link["url"] in seen or not title:
            continue
        item = source_item(
            title,
            link["url"],
            article["text"],
            venue_or_platform="The Met",
            city="New York",
        )
        start = parse_label_start_date(item.get("date_label"))
        if not start:
            # Met-specific trap: an open-ended opening prints year-less ("July 25-Ongoing"),
            # which detect_date_label can't anchor, so the row would silently vanish. We're
            # inside the #upcoming section, so the opening is future by definition — recover
            # the "Month D" from the article text and infer the next occurrence. (Friend's
            # "A Lasting Legacy" case: safe here precisely because section-awareness rules
            # out a past opening; a yearless date outside this section stays a review case.)
            ongoing = re.search(rf"({MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*[–-]\s*ongoing",
                                article["text"], re.I)
            if ongoing and not re.search(r"\b20\d{2}\b", article["text"]):
                start = next(
                    (d for y in (today().year, today().year + 1)
                     if (d := parse_us_date(f"{ongoing.group(1)} {ongoing.group(2)}, {y}")) and d >= today()),
                    None,
                )
                if start:
                    item["description"] = f"Source date: {normalize_space(ongoing.group(0))}"
                    item["date_label"] = f"{format_us_date(start)}–Ongoing"
                    item["date_precision"] = "exact"
        if not start or start < today() or start > end_date():
            continue
        item["date_start"] = start.isoformat()
        if item.get("date_label") and re.search(r"\bongoing\b", item["date_label"], re.I) \
                and not item.get("description", "").startswith("Source date:"):
            item["description"] = f"Source date: {item['date_label']}"
            item["date_label"] = normalize_space(re.split(r"\s*[–-]\s*ongoing\b", item["date_label"], flags=re.I)[0])
            item["date_precision"] = "exact"
        seen.add(link["url"])
        items.append(item)
        if len(items) >= limit:
            break
    return items


def met_opera_opening_date(date_label: str | None) -> dt.date | None:
    """Resolve a Met Opera label ("Sep 22 - Oct 10") to an opening date.

    The page omits years; the 2026-27 season runs Sep-Dec in 2026 and Jan-Aug in 2027.
    """
    if not date_label:
        return None
    match = re.match(rf"({MONTH_RE})\.?\s+(\d{{1,2}})", normalize_space(date_label), re.I)
    if not match:
        return None
    month = parse_month(match.group(1))
    year = 2026 if month >= 9 else 2027
    try:
        return dt.date(year, month, int(match.group(2)))
    except ValueError:
        return None


# Museums whose exhibition listings expose anchor-based detail links we can hydrate for
# real opening dates. (JS-rendered / bot-blocked museums use the browser-capture path.)
MUSEUMS = {
    "whitney": {"name": "Whitney Museum", "city": "New York",
                "link": r"https://whitney\.org/exhibitions/[a-z0-9-]+$"},
    "brooklyn_museum": {"name": "Brooklyn Museum", "city": "New York",
                        "link": r"https://www\.brooklynmuseum\.org/exhibitions/[a-z0-9-]+$"},
    "moca_la": {"name": "MOCA", "city": "Los Angeles",
                "link": r"https://www\.moca\.org/exhibition/[a-z0-9-]+$"},
    "pace_gallery": {"name": "Pace Gallery", "city": "New York",
                     "link": r"https://www\.pacegallery\.com/exhibitions/[a-z0-9-]+$"},
    "new_museum": {"name": "New Museum", "city": "New York", "json": True},
}


def parse_museum_json(source: Source, text: str) -> list[dict[str, Any]]:
    """Some venues embed exhibition data as JSON with ISO startDate (e.g. New Museum's
    GraphQL payload). Parse it directly — no detail hydration needed — keeping future
    openings within the horizon."""
    config = MUSEUMS[source.id]
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in re.finditer(
        r'"title":"([^"]+)","startDate":"(20\d\d-\d\d-\d\d)[^"]*","link":"([^"]+)"', text
    ):
        try:
            title = strip_tags(json.loads(f'"{match.group(1)}"'))
            url = json.loads(f'"{match.group(3)}"').split("?")[0].rstrip("/")
            start = dt.date.fromisoformat(match.group(2))
        except (ValueError, json.JSONDecodeError):
            continue
        if not title or url in seen or start < today() or start > end_date():
            continue
        seen.add(url)
        items.append(
            {
                "title": title,
                "date_start": start.isoformat(),
                "date_label": format_us_date(start),
                "date_precision": "exact",
                "venue_or_platform": config["name"],
                "city": config["city"],
                "source_url": url,
                "external_id": url,
            }
        )
    return items
MUSEUM_SKIP_SLUGS = {
    "performance", "archive", "upcoming", "past", "current", "on-view", "tour", "tours",
    "visit", "calendar", "all", "index", "membership", "the-biennial", "exhibitions",
}


def extract_exhibition_window(text: str) -> tuple[dt.date | None, str | None]:
    """Find an exhibition's opening date + label from detail-page text.

    Handles "Apr 30-Oct 19, 2026" (year only at the end), "Mar 6, 2026-Jul 12, 2026",
    explicit "Opens/On view ... 2026", a lone exact date, and a "Fall 2026" season. Returns
    (None, None) when only a closing ("Through ...") signal is present — i.e. already running.
    """
    text = normalize_space(text)
    opening = re.search(
        rf"(?:Open(?:s|ing)?|On view(?:\s+(?:from|beginning))?)\s+({MONTH_RE}\.?\s+\d{{1,2}},?\s+20\d{{2}})",
        text, re.I,
    )
    if opening:
        parsed = parse_us_date(opening.group(1))
        if parsed:
            return parsed, normalize_space(opening.group(1))
    end_year_range = re.search(
        rf"({MONTH_RE}\.?\s+\d{{1,2}})\s*[–—-]\s*({MONTH_RE}\.?\s+\d{{1,2}}),?\s+(20\d{{2}})", text, re.I,
    )
    if end_year_range:
        parsed = parse_us_date(f"{end_year_range.group(1)}, {end_year_range.group(3)}")
        if parsed:
            return parsed, normalize_space(end_year_range.group(0))
    full_range = re.search(
        rf"({MONTH_RE}\.?\s+\d{{1,2}},?\s+20\d{{2}})\s*[–—-]\s*(?:{MONTH_RE}\.?\s+\d{{1,2}},?\s+20\d{{2}})", text, re.I,
    )
    if full_range:
        parsed = parse_us_date(full_range.group(1))
        if parsed:
            return parsed, normalize_space(full_range.group(0))
    single = re.search(rf"({MONTH_RE}\.?\s+\d{{1,2}},?\s+20\d{{2}})", text, re.I)
    if single and not re.search(r"\b(through|closing|closes|ends|on view through)\b", text[:single.start()], re.I):
        parsed = parse_us_date(single.group(1))
        if parsed:
            return parsed, normalize_space(single.group(1))
    season = re.search(r"\b(Spring|Summer|Fall|Autumn|Winter)\s+20\d{2}\b", text, re.I)
    if season:
        return None, normalize_space(season.group(0))
    return None, None


def parse_museum_listing(source: Source, text: str) -> list[dict[str, Any]]:
    config = MUSEUMS[source.id]
    # Treat a dedicated upcoming page (config "upcoming") or an explicit "Upcoming" section
    # (Whitney, Met-style) as all-upcoming — so entries with no announced date yet (e.g.
    # Whitney's Lichtenstein) are kept rather than dropped.
    upcoming_only = config.get("upcoming", False)
    section = re.search(r'<section[^>]*id="upcoming"', text, re.I)
    if section:
        upcoming_only = True
        segment = text[section.start():]
        past = re.search(r'id="past"', segment, re.I)
        text = segment[: past.start()] if past else segment
    parser = LinkTextParser(source.url)
    parser.feed(text)
    pattern = re.compile(config["link"])
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in parser.links:
        url = link["url"].split("?")[0].rstrip("/")
        if not pattern.match(url) or url in seen:
            continue
        if url.rsplit("/", 1)[-1] in MUSEUM_SKIP_SLUGS:
            continue
        title = normalize_space(link["text"])
        if not title or len(title) < 3:
            continue
        seen.add(url)
        items.append(
            {
                "title": title,
                "venue_or_platform": config["name"],
                "city": config["city"],
                "source_url": url,
                "external_id": url,
                "upcoming": upcoming_only,
            }
        )
    return items


def hydrate_museum_dates(conn: sqlite3.Connection, source: Source, items: list[dict[str, Any]], limit: int = 50) -> list[dict[str, Any]]:
    """Follow each exhibition page for its opening date; keep only future/seasonal openings
    within the horizon (drops already-running and beyond-2026 shows)."""
    kept: list[dict[str, Any]] = []
    for item in items[:limit]:
        try:
            html_text = fetch_text(item["source_url"])
            raw_path = save_detail(source, item["external_id"], html_text)
            upsert_detail(conn, source, item, html_text, raw_path)
        except Exception:
            continue
        meta = MetaParser()
        meta.feed(html_text)
        clean_title = meta.meta.get("og:title") or meta.title or item["title"]
        clean_title = re.split(r"\s*[|]\s*", clean_title)[0]  # drop " | Whitney Museum" suffix
        if clean_title and len(normalize_space(clean_title)) >= 3:
            item["title"] = normalize_space(clean_title)
        start, label = extract_exhibition_window(strip_tags(html_text)[:2500])
        if start and today() <= start <= end_date():
            item["date_start"] = start.isoformat()
            item["date_label"] = label or format_us_date(start)
            item["date_precision"] = "exact"
            kept.append(item)
        elif not start and label and "2026" in label:  # future season within the 2026 horizon
            item["date_label"] = label
            item["date_precision"] = "season"
            kept.append(item)
        elif not start and item.get("upcoming"):
            # Known-upcoming show with no announced date (from the listing's Upcoming
            # section) — keep it as a horizon item rather than dropping it.
            item["date_label"] = "Upcoming"
            item["date_precision"] = "tba"
            kept.append(item)
        # else: no future signal (already open, past, or beyond 2026) -> drop
    return kept


# Perelman Performing Arts Center (PAC NYC) is multidisciplinary; map its genre tag to the
# calendar's category so operas land in Opera, concerts in Music, plays in Theatre, etc.
PAC_GENRE_CATEGORY = {
    "opera": "opera",
    "music": "music",
    "dance": "ballet",
    "film": "film",
    "theater": "theatre",
    "musical-theater": "theatre",
    "multi-disciplinary": "theatre",
    "conversation": "theatre",
}


def extract_pac_date(text: str) -> tuple[dt.date | None, str | None]:
    """Resolve a PAC event's opening date from its date-range element text.

    PAC writes the date several ways: "Sep 13, 2026"; a run "Jun 28—Jul 26, 2026" (the
    trailing year governs the opening); year-less "June 20 at 7pm" / "Begins July 11, ...";
    and a cross-year run "Nov 20, 2026—Jan 3, 2027". The opening is the first month/day in
    the text, taking the first explicit year present or, when none, the next future year.
    Horizon/already-running filtering is the caller's job — a run whose opening is in the
    past is currently running, not upcoming.
    """
    flat = normalize_space(text)
    day_match = re.search(rf"({MONTH_RE})\.?\s+(\d{{1,2}})", flat, re.I)
    if not day_match:
        return None, None
    month, day = day_match.group(1), day_match.group(2)
    year_match = re.search(r"\b(20\d{2})\b", flat)
    if year_match:
        opening = parse_us_date(f"{month} {day}, {year_match.group(1)}")
    else:
        opening = next(
            (d for y in (today().year, today().year + 1)
             if (d := parse_us_date(f"{month} {day}, {y}")) and d >= today()),
            None,
        )
    if not opening:
        return None, None
    return opening, format_us_date(opening)


def import_pac(conn: sqlite3.Connection, source: Source, limit: int = 40) -> int:
    """Perelman Performing Arts Center (PAC NYC) — the multidisciplinary World Trade Center
    venue. The What's On listing exposes /whats-on/<slug>/ event links; each detail page
    carries the discipline (a /whats-on/genres/<genre>/ link) and the performance date. We
    hydrate each detail page, map genre -> category, and keep future-opening events only.
    NYC by definition."""
    text = fetch_text(source.url)
    raw_path = save_raw(source, text)
    urls: list[str] = []
    seen: set[str] = set()
    for href in re.findall(r'href="(https://pacnyc\.org/whats-on/[^"#?]+)"', text):
        url = href.split("?")[0].rstrip("/")
        if "/genres/" in url or url.rsplit("/", 1)[-1] in {"whats-on", ""} or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    count = 0
    for url in urls[:limit]:
        try:
            detail = fetch_text(url + "/")
            save_detail(source, url, detail)
        except Exception:
            continue
        # Scope to the canonical date element; the whole page repeats other shows' dates
        # (sidebars/related events). If PAC ever drops it, skip rather than mis-date.
        daterange = re.search(r'c-event-details__daterange"[^>]*>\s*([^<]+)', detail)
        start, label = extract_pac_date(daterange.group(1) if daterange else "")
        if not start or start < today() or start > end_date():
            continue  # undated, already running, or beyond the 2026 horizon
        genres = re.findall(r"/whats-on/genres/([a-z-]+)/", detail)
        genre = next((g for g in genres if g in PAC_GENRE_CATEGORY), genres[0] if genres else "")
        meta = MetaParser()
        meta.feed(detail)
        title = normalize_space(re.split(r"\s*\|\s*", meta.meta.get("og:title") or meta.title or "")[0])
        if not title or len(title) < 3:
            continue
        item = {
            "title": title,
            "category": PAC_GENRE_CATEGORY.get(genre, "theatre"),
            "date_start": start.isoformat(),
            "date_label": label,
            "date_precision": "exact",
            "venue_or_platform": "Perelman Performing Arts Center",
            "city": "New York",
            "source_url": url,
            "external_id": url,
            "description": f"PAC NYC, {genre.replace('-', ' ') or 'performance'}",
            "importance_score": 12,
        }
        upsert_item(conn, source, item)
        ensure_model_enrichment_placeholder(conn, source, item)
        count += 1
    record_run(conn, source, "ok", f"imported {count} upcoming events", raw_path)
    return count


# The Shed (Hudson Yards) is multidisciplinary with no per-event genre in its markup, so we
# classify discipline from the title/tagline, defaulting to theatre (its most common form).
SHED_CATEGORY_KEYWORDS = (
    # Music is checked first: "Lightscape: <performer>" is a concert series, while the standalone
    # "Doug Aitken: Lightscape" is the art installation (caught by "aitken"/"installation").
    ("music", ("in concert", "concert", "symphony", "orchestra", "arkestra", "quartet",
               "recital", "lightscape:", "chorale")),
    ("art", ("exhibition", "frieze", "aitken", "installation", "retrospective",
             "paintings", "sculpture", "art fair")),
    ("ballet", ("dance", "ballet", "choreograph")),
    ("film", ("film", "screening", "cinema")),
)
# Listing rows that aren't programmable cultural events.
SHED_SKIP = ("open call", "application", "membership", "donate", "gift card", "rental",
             "private event", "venue hire")


def shed_category(text: str) -> str:
    lowered = text.lower()
    for category, keywords in SHED_CATEGORY_KEYWORDS:
        if any(k in lowered for k in keywords):
            return category
    return "theatre"


def shed_opening_date(label: str) -> tuple[dt.date | None, str | None]:
    """The Shed prints "JUN 20 – SEP 6" (uppercase, usually no year). Take the first month/day
    as the opening, using an explicit year if present or the next future one otherwise."""
    match = re.search(rf"({MONTH_RE})\.?\s+(\d{{1,2}})", label, re.I)
    if not match:
        return None, None
    month, day = match.group(1).title(), match.group(2)
    year_match = re.search(r"20\d{2}", label)
    if year_match:
        opening = parse_us_date(f"{month} {day}, {year_match.group(0)}")
    else:
        opening = next((d for y in (today().year, today().year + 1)
                        if (d := parse_us_date(f"{month} {day}, {y}")) and d >= today()), None)
    return (opening, format_us_date(opening)) if opening else (None, None)


def import_the_shed(conn: sqlite3.Connection, source: Source, limit: int = 40) -> int:
    """The Shed — its program page is server-rendered with an #upcoming section of cards
    (title + date range). Parse that section, infer the opening year, classify discipline from
    the title/tagline, keep future-opening events within the horizon. NYC by definition."""
    text = fetch_text(source.url)
    raw_path = save_raw(source, text)
    upcoming = text[text.find("id='upcoming'"):]
    past = upcoming.find("id='past'")
    if past > 0:
        upcoming = upcoming[:past]
    cards = re.findall(
        r'event__link"\s+href="(/program/[^"]+)".*?'
        r"event__title'>(.*?)</h2>\s*<h3 class='event__date'>(.*?)</h3>"
        r"(?:\s*<p class='event__tagline'>(.*?)</p>)?",
        upcoming, re.S,
    )
    count = 0
    seen: set[str] = set()
    for href, title_raw, date_raw, tagline_raw in cards:
        title = normalize_space(html.unescape(strip_tags(title_raw)))
        if not title or href in seen or any(s in title.lower() for s in SHED_SKIP):
            continue
        start, label = shed_opening_date(normalize_space(html.unescape(strip_tags(date_raw))))
        if not start or start < today() or start > end_date():
            continue
        seen.add(href)
        tagline = normalize_space(html.unescape(strip_tags(tagline_raw or "")))
        url = "https://theshed.org" + href.split("?")[0]
        item = {
            "title": title,
            "category": shed_category(f"{title} {tagline}"),
            "date_start": start.isoformat(),
            "date_label": label,
            "date_precision": "exact",
            "venue_or_platform": "The Shed",
            "city": "New York",
            "source_url": url,
            "external_id": url,
            "description": tagline or "The Shed, Hudson Yards",
            "importance_score": 12,
        }
        upsert_item(conn, source, item)
        ensure_model_enrichment_placeholder(conn, source, item)
        count += 1
    record_run(conn, source, "ok", f"imported {count} upcoming events", raw_path)
    return count


def parse_met_opera(source: Source, text: str, limit: int = 80) -> list[dict[str, Any]]:
    parser = LinkTextParser(source.url)
    parser.feed(text)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    suffix = " Buy Tickets and learn more about this production"
    for link in parser.links:
        url = link["url"]
        raw = link["text"].replace(suffix, "")
        if "/season/2026-27-season/" not in url and "/season/2026-27-special-presentation/" not in url:
            continue
        if raw in {"Skip to main content", "2026–27 Season"} or "Live Chat" in raw:
            continue
        if url.rstrip("/") == source.url.rstrip("/"):
            continue
        if url in seen:
            continue
        seen.add(url)
        new_production = raw.startswith("New Production ")
        raw = re.sub(r"^New Production\s+", "", raw)
        date_match = re.search(r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}\s*[-–]\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2})", raw)
        date_label = date_match.group(1) if date_match else None
        title = raw[date_match.end():].strip() if date_match else raw.strip()
        if not title:
            title = link["text"]
        description = "New Production" if new_production else None
        opening = met_opera_opening_date(date_label)
        # Set a sortable date_start only for openings inside the 2026 horizon; spring-2027
        # openings stay label-only (still shown, just not as dated 2026 rows).
        in_horizon = bool(opening and today() <= opening <= end_date())
        items.append(
            {
                "title": normalize_space(title),
                "date_start": opening.isoformat() if in_horizon else None,
                "date_label": normalize_space(date_label) if date_label else None,
                "date_precision": "exact" if in_horizon else ("exact_or_range" if date_label else "unknown"),
                "venue_or_platform": "Metropolitan Opera",
                "city": "New York",
                "source_url": url,
                "external_id": url,
                "description": description,
                "importance_score": 15 if new_production else 5,
            }
        )
        if len(items) >= limit:
            break
    return items


# Creative roles (lowercased) that are NOT the stage director, so we don't mistake one for it.
MET_OPERA_NON_DIRECTOR = {
    "movement director", "chorus director", "music director", "associate director",
    "assistant director", "revival stage director", "fight director", "children's chorus director",
}


def extract_met_opera_credits(html_text: str) -> list[dict[str, str]]:
    """Director, conductor, and the two top-billed principal singers from a Met production page.

    The Met labels the stage director "Production" in its creator list; the conductor and
    singers come from the page's entity-encoded cast JSON (each carries a name + a
    numberlessRole — "Conductor" or the character a singer performs). We keep the first two
    distinct character roles (deduping alternate casts) as the leads.
    """
    people: list[dict[str, str]] = []
    # Two markup variants: role in a "...role" <p> then a creator-list-detail-name <p>
    # (e.g. Medea), or a plain role <p> then a creator-artist-info-name <p> (e.g. Macbeth).
    creative = re.findall(
        r'>\s*([^<>]+?)\s*</p>\s*<p class="creator-(?:list-detail|artist-info)-name">\s*([^<]+?)\s*</p>',
        html_text,
    )
    director = None
    for role, name in creative:
        role, name = normalize_space(html.unescape(role)), normalize_space(html.unescape(name))
        role_l = role.lower()  # the Met uses both "Production" and "PRODUCTION"
        if not name or role_l in MET_OPERA_NON_DIRECTOR:
            continue
        if role_l == "production":  # the Met's term for the stage director
            director = name
            break
        if director is None and (role_l == "director" or role_l.endswith("stage director")):
            director = name
    if director:
        people.append({"name": director, "role": "Director"})
    members = re.findall(
        r'"name":"([^"]+)"(?:(?!"name":).)*?"numberlessRole":"([^"]+)"',
        html.unescape(html_text), re.S,
    )
    conductor_done = False
    leads: list[str] = []
    seen_roles: set[str] = set()
    for name, role in members:
        name, role = normalize_space(name), normalize_space(role)
        if role == "Conductor":
            if not conductor_done and name:
                people.append({"name": name, "role": "Conductor"})
                conductor_done = True
            continue
        if role in seen_roles or not name:
            continue
        seen_roles.add(role)  # dedupe alternate casts of the same character
        if len(leads) < 2:
            leads.append(name)
    people.extend({"name": n, "role": "Cast"} for n in leads)
    return people


def hydrate_met_opera_credits(source: Source, items: list[dict[str, Any]]) -> None:
    """Follow each in-horizon production page for its director/conductor/lead-singer credits.
    Runs before the capture fixture is written, so the CI fallback carries credits too."""
    for item in items:
        if not item.get("date_start"):
            continue  # only the productions that actually render are worth hydrating
        try:
            page = fetch_text(item["source_url"])
        except Exception:
            continue
        people = extract_met_opera_credits(page)
        if people:
            item["people"] = people


def parse_nycb(source: Source, text: str, limit: int = 120) -> list[dict[str, Any]]:
    parser = LinkTextParser(source.url)
    parser.feed(text)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    season_labels = {
        "fall-2026": "Fall 2026",
        "winter-2027": "Winter 2027",
        "spring-2027": "Spring 2027",
    }
    skip_titles = {"Fall 2026", "Winter 2027", "Spring 2027"}
    for link in parser.links:
        url = link["url"]
        match = re.search(r"/season-and-tickets/(fall-2026|winter-2027|spring-2027)/([^/?#]+)$", url)
        if not match or link["text"] in skip_titles or url in seen:
            continue
        seen.add(url)
        season = season_labels[match.group(1)]
        items.append(
            {
                "title": normalize_space(link["text"]),
                "date_label": season,
                "date_precision": "season",
                "venue_or_platform": "New York City Ballet",
                "city": "New York",
                "source_url": url,
                "external_id": url,
                "description": season,
            }
        )
        if len(items) >= limit:
            break
    return items


def parse_links(source: Source, text: str, limit: int = 80) -> list[dict[str, Any]]:
    parser = LinkTextParser(source.url)
    parser.feed(text)
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for link in parser.links:
        title = link["text"]
        if len(title) < 3 or len(title) > 220:
            continue
        if source.id == "broadway_org" and not link["url"].startswith("https://www.broadway.org/shows/"):
            continue
        if source.id == "playbill_broadway" and "playbill.com" not in link["url"]:
            continue
        if source.id == "met_exhibitions" and "/exhibitions/" not in link["url"]:
            continue
        if source.id == "moma_exhibitions" and "/calendar/exhibitions/" not in link["url"]:
            continue
        if source.id == "met_opera_2026_27" and "season" not in link["url"]:
            continue
        if source.id == "nycb_seasons" and "season-and-tickets" not in link["url"]:
            continue

        _, _, date_label = detect_date_label(title)
        key = (title, link["url"])
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "title": title,
                "date_label": date_label,
                "date_precision": label_precision(date_label),
                "source_url": link["url"],
                "external_id": link["url"],
            }
        )
        if len(items) >= limit:
            break
    return items


def source_country_code(show: dict[str, Any]) -> str | None:
    channel = show.get("network") or show.get("webChannel") or {}
    country = channel.get("country") if isinstance(channel, dict) else None
    return country.get("code") if isinstance(country, dict) else None


def tv_channel_name(show: dict[str, Any]) -> str | None:
    channel = show.get("network") or show.get("webChannel") or {}
    return channel.get("name") if isinstance(channel, dict) else None


def tv_importance_score(episode: dict[str, Any], show: dict[str, Any]) -> int:
    channel = tv_channel_name(show) or ""
    show_type = show.get("type") or ""
    genres = set(show.get("genres") or [])
    season = episode.get("season")
    number = episode.get("number")
    airdate = episode.get("airdate")
    premiered = show.get("premiered")

    major_platforms = {
        "HBO": 65,
        "HBO Max": 60,
        "Max": 60,
        "FX": 58,
        "Netflix": 55,
        "Apple TV+": 55,
        "Apple TV": 55,
        "Hulu": 50,
        "Prime Video": 50,
        "Amazon Prime Video": 50,
        "Disney+": 48,
        "Paramount+": 45,
        "Peacock": 42,
        "Showtime": 42,
        "PBS": 38,
        "AMC": 38,
        "BBC America": 34,
        "NBC": 30,
        "ABC": 30,
        "CBS": 30,
        "FOX": 30,
        "The CW": 18,
        "Dropout": 16,
    }
    type_weights = {
        "Scripted": 28,
        "Documentary": 24,
        "Animation": 16,
        "Reality": -12,
        "Talk Show": -25,
        "News": -35,
        "Game Show": -20,
        "Sports": -30,
    }
    score = major_platforms.get(channel, 8)
    score += type_weights.get(show_type, 0)
    if season == 1:
        score += 18
    if number == 1:
        score += 14
    if airdate and premiered and airdate == premiered:
        score += 12
    if {"Drama", "Crime", "Thriller", "Science-Fiction", "Mystery"} & genres:
        score += 8
    if {"Food", "DIY", "Travel", "Legal"} & genres:
        score -= 8
    return max(score, 0)


def is_relevant_tv_episode(episode: dict[str, Any], show: dict[str, Any], aperture: str) -> bool:
    country = source_country_code(show)
    web_channel = show.get("webChannel") or {}
    is_global_streamer = isinstance(web_channel, dict) and web_channel.get("country") is None
    major_global_streamers = {
        "Netflix",
        "Prime Video",
        "Amazon Prime Video",
        "Apple TV+",
        "Disney+",
        "Hulu",
        "Max",
        "HBO Max",
        "Peacock",
        "Paramount+",
        "Dropout",
    }
    if country != "US" and not (is_global_streamer and web_channel.get("name") in major_global_streamers):
        return False
    if show.get("language") not in {None, "English"}:
        return False

    number = episode.get("number")
    season = episode.get("season")
    airdate = episode.get("airdate")
    premiered = show.get("premiered")
    is_start_signal = number == 1 or (airdate and premiered and airdate == premiered and number in {1, None})

    if aperture == "wide":
        return is_start_signal and tv_importance_score(episode, show) >= 20

    # For an editorial planning calendar, starts are more useful than every episode.
    return (
        is_start_signal
        and tv_importance_score(episode, show) >= 50
    )


def tvmaze_principals(show_id: Any, cache: dict[Any, list[dict[str, str]]]) -> list[dict[str, str]]:
    """Creators/EPs then top billed cast for a TVmaze show (free, no key), role-labeled."""
    if show_id in cache:
        return cache[show_id]
    people: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(name: str | None, role: str) -> None:
        if name and name not in seen:
            people.append({"name": name, "role": role})
            seen.add(name)

    try:
        # The authorial signal is the Creator (wrote the pilot) or an *explicitly* labeled
        # showrunner. A bare "Executive Producer" can mean almost anything, so it's excluded.
        crew = json.loads(fetch_text(f"https://api.tvmaze.com/shows/{show_id}/crew"))
        for member in crew:
            crew_type = (member.get("type") or "")
            if crew_type == "Creator":
                add((member.get("person") or {}).get("name"), "Creator")
            elif "showrunner" in crew_type.lower():
                add((member.get("person") or {}).get("name"), "Showrunner")
    except Exception:
        pass
    try:
        data = json.loads(fetch_text(f"https://api.tvmaze.com/shows/{show_id}?embed=cast"))
        for member in (data.get("_embedded", {}).get("cast") or [])[:6]:
            add((member.get("person") or {}).get("name"), "Cast")
    except Exception:
        pass
    cache[show_id] = people
    return people


def tmdb_screenwriters(crew: list[dict[str, Any]]) -> list[str]:
    """Screenwriter names from a TMDb crew list. Prefer the 'Screenplay' job; fall back to the
    generic 'Writer'. Source-material credits ("Novel", "Story", "Author", "Characters", …) are
    NOT screenwriters — e.g. Homer is credited on Nolan's Odyssey as the source, not the writer."""
    screenplay = [m.get("name") for m in crew if m.get("job") == "Screenplay" and m.get("name")]
    if screenplay:
        return screenplay
    return [m.get("name") for m in crew if m.get("job") == "Writer" and m.get("name")]


def tmdb_principals(movie_id: Any, token: str) -> list[dict[str, str]]:
    """Director, writer(s), then top billed cast for a TMDb movie, each role-labeled."""
    people: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(name: str | None, role: str) -> None:
        if name and name not in seen:
            people.append({"name": name, "role": role})
            seen.add(name)

    try:
        data = json.loads(
            fetch_text(
                f"https://api.themoviedb.org/3/movie/{movie_id}/credits",
                headers={"Authorization": f"Bearer {token}"},
            )
        )
        for member in data.get("crew", []):
            if member.get("job") == "Director":
                add(member.get("name"), "Director")
        for name in tmdb_screenwriters(data.get("crew", [])):
            add(name, "Writer")
        for member in (data.get("cast") or [])[:5]:
            add(member.get("name"), "Cast")
    except Exception:
        pass
    return people


# Carnegie's /events is DataDome-protected, but its data is an Algolia index the page
# queries with a public, referer-restricted search key. Querying Algolia directly (with a
# Referer header) is fully scriptable — no browser needed. These are public client values
# (visible in the page source); rediscover via the browser if Carnegie rotates them.
CARNEGIE_ALGOLIA = {
    "host": "q0tmlopf1j-dsn.algolia.net",
    "app_id": "Q0TMLOPF1J",
    "api_key": "d2d2b382f2659c44ef8927aad7a24172",
    "index": "prod_Events",
}
CARNEGIE_HALLS = re.compile(r"Stern Auditorium|Perelman Stage|Zankel Hall")
CARNEGIE_RENTAL_MILLS = re.compile(
    r"MidAmerica|Manhattan Concert|National Concerts|DCINY|Distinguished Concerts|"
    r"AWR Music|Concerti Sinfonietta|Perfect Harmony",
    re.I,
)


def import_carnegie(conn: sqlite3.Connection, source: Source) -> int:
    """Carnegie Hall concerts via direct Algolia query — marquee halls, Carnegie's own
    programming (rental mills dropped). Repeatable/automatable; no browser."""
    cfg = CARNEGIE_ALGOLIA
    now_ms = int(dt.datetime.now().timestamp() * 1000)
    end_ms = int(dt.datetime(end_date().year, 12, 31).timestamp() * 1000)
    params = "hitsPerPage=1000&numericFilters=" + json.dumps([f"startdate>={now_ms}", f"startdate<={end_ms}"])
    body = json.dumps({"requests": [{"indexName": cfg["index"], "params": params}]})
    response = requests.post(
        f"https://{cfg['host']}/1/indexes/*/queries",
        data=body,
        headers={
            "X-Algolia-API-Key": cfg["api_key"],
            "X-Algolia-Application-Id": cfg["app_id"],
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://www.carnegiehall.org/",
            "Origin": "https://www.carnegiehall.org",
        },
        timeout=30,
    )
    response.raise_for_status()
    raw_path = save_raw(source, response.text)
    hits = json.loads(response.text)["results"][0]["hits"]
    count = 0
    seen: set[str] = set()
    for hit in hits:
        if not CARNEGIE_HALLS.search(hit.get("facility", "")):
            continue
        if CARNEGIE_RENTAL_MILLS.search(hit.get("licenseename", "")):
            continue
        try:
            start = dt.date.fromtimestamp(hit["startdate"] / 1000)
        except (KeyError, TypeError, ValueError, OSError):
            continue
        if start < today() or start > end_date():
            continue
        title = strip_tags(hit.get("title", ""))
        if not title:
            continue
        key = f"{start.isoformat()}|{title.lower()}"
        if key in seen:
            continue
        seen.add(key)
        performers = [
            p for p in (strip_tags(x) for x in re.split(r"<br\s*/?>|[\r\n]+", hit.get("webdisplayperformers") or "")) if p
        ][:4]
        people = [{"name": p, "role": "Performer"} for p in performers if p.lower() not in title.lower()]
        item = {
            "title": title,
            "category": "music",
            "date_start": start.isoformat(),
            "date_label": format_us_date(start),
            "date_precision": "exact",
            "venue_or_platform": "Carnegie Hall",
            "city": "New York",
            "source_url": urljoin("https://www.carnegiehall.org", hit.get("url", "/events")),
            "external_id": f"carnegie:{key[:60]}",
            "people": people,
            "description": f"Carnegie Hall — {hit.get('facility','')}".strip(" —"),
            "importance_score": 16,
        }
        upsert_item(conn, source, item)
        ensure_model_enrichment_placeholder(conn, source, item)
        count += 1
    record_run(conn, source, "ok", f"imported {count} concerts via Algolia", raw_path)
    return count


# NY Phil's calendar (a JS app) is fed by a public, CORS-open CloudFront endpoint the page
# itself calls — no auth, no browser. The path segments are ignored by the backend; it
# returns the full live event list (the rest of this season + next), already collapsing
# multi-night subscription runs into one event with a DateDescription range. So this is a
# plain json_api source now, replacing the old browser-capture fixture.
NYPHIL_API_URL = "https://d1c3g0ihb82aph.cloudfront.net/Prod/events/9/2/none/live"
# Concerts are NYC-only (user decision): drop the summer Bravo! Vail residency and any other
# out-of-town venue (state abbreviations).
NYPHIL_NON_NYC = re.compile(r"\bVail\b|,\s*(?:CO|NJ|CT|MA|PA|FL|CA|DC|VA|RI|NH|VT)\b", re.I)


def _nyphil_end_date(start: dt.date, desc: str | None) -> dt.date | None:
    """Parse the run-end date from a DateDescription range like 'Sep 16–Sep 19' or
    'Nov 25–Dec 1'. Returns None for single-day events or unparseable labels."""
    if not desc:
        return None
    parts = re.split(r"\s*[–—-]\s*", desc.strip())
    if len(parts) != 2:
        return None
    m = re.search(r"([A-Za-z]+)?\.?\s*(\d{1,2})", parts[1])
    if not m:
        return None
    month = MONTH_NUMBERS.get((m.group(1) or "").strip(".").lower(), start.month)
    try:
        end = dt.date(start.year, month, int(m.group(2)))
    except ValueError:
        return None
    if end < start:  # range crosses into the next year (e.g. Dec 30 – Jan 2)
        try:
            end = end.replace(year=start.year + 1)
        except ValueError:
            return None
    return end if end >= start else None


def parse_nyphil_events(events: list[dict[str, Any]], limit: int = 200) -> list[dict[str, Any]]:
    """Pure parser for the NY Phil events API payload (testable without the network)."""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        if not event.get("ShowInCalendar"):
            continue
        raw_start = event.get("StartDate")
        if not raw_start:
            continue
        try:
            start = dt.date.fromisoformat(raw_start[:10])
        except ValueError:
            continue
        if start < today() or start > end_date():
            continue
        venue = normalize_space(event.get("Venue") or "")
        if NYPHIL_NON_NYC.search(venue):
            continue
        title = normalize_space(html.unescape(strip_tags(event.get("StrippedTitle") or event.get("Title") or "")))
        if not title:
            continue
        ext = f"nyphil:{event.get('ID') or title.lower()}"
        if ext in seen:
            continue
        seen.add(ext)
        desc = normalize_space(html.unescape(event.get("DateDescription") or ""))
        end = _nyphil_end_date(start, desc)
        date_label = desc or format_us_date(start)
        items.append(
            {
                "title": title,
                "category": "music",
                "date_start": start.isoformat(),
                "date_end": end.isoformat() if end and end != start else None,
                "date_label": date_label,
                "date_precision": "exact",
                "venue_or_platform": "New York Philharmonic",
                "city": "New York",
                "source_url": event.get("EventLink") or "https://www.nyphil.org/concerts-tickets/calendar/",
                "external_id": ext,
                "description": f"New York Philharmonic — {venue}".rstrip(" —") if venue else "New York Philharmonic",
                "importance_score": 16,
            }
        )
        if len(items) >= limit:
            break
    return items


def import_nyphil_api(conn: sqlite3.Connection, source: Source) -> int:
    """NY Philharmonic concerts via the public CloudFront events API (no browser, no fixture).

    The CloudFront/WAF in front of the API TLS-fingerprints urllib3 and 403s it, so we go
    through fetch_text, which falls back to curl on a 403 (same tactic as Metacritic)."""
    text = fetch_text(NYPHIL_API_URL, headers={"Accept": "application/json"})
    raw_path = save_raw(source, text)
    items = parse_nyphil_events(json.loads(text))
    for item in items:
        upsert_item(conn, source, item)
        ensure_model_enrichment_placeholder(conn, source, item)
    record_run(conn, source, "ok", f"imported {len(items)} concerts via NY Phil API", raw_path)
    return len(items)


# Marquee galleries are Next.js: their exhibitions sit in the page's __NEXT_DATA__ JSON,
# so they're scriptable via a plain fetch. Per-gallery config maps the JSON path + fields.
GALLERIES = {
    "gagosian": {
        "name": "Gagosian", "url": "https://gagosian.com/exhibitions/upcoming/", "base": "https://gagosian.com",
        "path": ["props", "pageProps", "exhibitions"],
        "title": "title", "dates": "dates_display", "loc": "location_str", "link": "absolute_url",
    },
}


def import_gallery_nextdata(conn: sqlite3.Connection, source: Source) -> int:
    """Gallery exhibitions from the page's __NEXT_DATA__ JSON. NY-only, future-opening."""
    cfg = GALLERIES[source.id]
    text = fetch_text(cfg["url"])
    raw_path = save_raw(source, text)
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
    if not match:
        record_run(conn, source, "error", "no __NEXT_DATA__", raw_path)
        return 0
    node: Any = json.loads(match.group(1))
    for key in cfg["path"]:
        node = node.get(key, {}) if isinstance(node, dict) else {}
    exhibitions = node if isinstance(node, list) else []
    count = 0
    seen: set[str] = set()
    for ex in exhibitions:
        location = str(ex.get(cfg["loc"]) or "")
        if "New York" not in location:  # NY-only per editorial scope
            continue
        title = strip_tags(str(ex.get(cfg["title"]) or ""))
        if not title:
            continue
        start, _ = extract_exhibition_window(str(ex.get(cfg["dates"]) or ""))
        if not start or start < today() or start > end_date():  # future-opening only
            continue
        url = urljoin(cfg["base"], str(ex.get(cfg["link"]) or ""))
        if url in seen:
            continue
        seen.add(url)
        item = {
            "title": title,
            "date_start": start.isoformat(),
            "date_label": normalize_space(str(ex.get(cfg["dates"]) or format_us_date(start))),
            "date_precision": "exact",
            "venue_or_platform": cfg["name"],
            "city": "New York",
            "source_url": url,
            "external_id": url,
            "description": f"{cfg['name']}, New York",
        }
        upsert_item(conn, source, item)
        ensure_model_enrichment_placeholder(conn, source, item)
        count += 1
    record_run(conn, source, "ok", f"imported {count} NY exhibitions from __NEXT_DATA__", raw_path)
    return count


def import_guggenheim(conn: sqlite3.Connection, source: Source) -> int:
    """Guggenheim (WordPress) embeds its exhibitions as JSON in the listing page, with
    structured `dates.start {day, month, year}`. Parse it directly; keep future openings."""
    text = fetch_text(source.url)
    raw_path = save_raw(source, text)
    decoder = json.JSONDecoder()
    count = 0
    seen: set[str] = set()
    for section in ("on_view", "upcoming"):
        marker = f'"{section}":'
        idx = text.find(marker)
        if idx < 0:
            continue
        try:
            obj, _ = decoder.raw_decode(text[idx + len(marker):])
        except ValueError:
            continue
        for item in obj.get("items", []):
            start_raw = (item.get("dates") or {}).get("start") or {}
            try:
                start = dt.date(int(start_raw["year"]), parse_month(start_raw["month"]), int(start_raw["day"]))
            except (KeyError, ValueError, TypeError):
                continue
            if start < today() or start > end_date():  # future-opening only
                continue
            title = strip_tags(item.get("title", ""))
            if not title:
                continue
            slug = item.get("slug", "")
            url = f"https://www.guggenheim.org/exhibition/{slug}" if slug else source.url
            if url in seen:
                continue
            seen.add(url)
            record = {
                "title": title,
                "date_start": start.isoformat(),
                "date_label": format_us_date(start),
                "date_precision": "exact",
                "venue_or_platform": "Guggenheim",
                "city": "New York",
                "source_url": url,
                "external_id": url,
                "description": "Guggenheim, New York",
            }
            upsert_item(conn, source, record)
            ensure_model_enrichment_placeholder(conn, source, record)
            count += 1
    record_run(conn, source, "ok", f"imported {count} upcoming exhibitions", raw_path)
    return count


def tate_opening_date(text: str) -> tuple[dt.date | None, str | None]:
    """Parse Tate's UK day-first run label into an opening date. "25 Jun 2026 – 3 Jan 2027"
    and "3 June – 23 August 2026" (year only at the range end) give an opening; "Until ..."
    (closing-only = currently running), recurring/ongoing tour-and-talk text, and a bare year
    give none — which is how exhibitions are separated from events and running shows."""
    if re.match(r"\s*until\b", text, re.I):
        return None, None
    day_month = re.search(r"(\d{1,2})\s+([A-Za-z]+)", text)
    if not day_month:
        return None, None
    day, month = day_month.group(1), day_month.group(2)
    year = re.search(rf"{re.escape(day_month.group(0))}\s+(20\d\d)", text) or re.search(r"(20\d\d)", text)
    if not year:
        return None, None
    opening = parse_us_date(f"{month} {day}, {year.group(1)}")
    return (opening, normalize_space(text)) if opening else (None, None)


def import_with_cache(conn: sqlite3.Connection, source: Source, cache_path: Path,
                      parser: "callable", must_contain: tuple[str, ...] = ()) -> int:
    """Run a live scraper backed by a committed last-good cache, enforcing the integrity rule:
    a failed scrape makes data STALE, never empty. Validate the fetch (fetch_valid_page); on a
    blocked/challenge/truncated/wrong-shape page, serve the cache and record 'stale'. On a clean
    fetch, merge live results with the cache, refresh the cache, and record 'ok'. A clean fetch
    that parses nothing keeps the cache (flagged) so a parser/shape change can't silently zero a
    venue — zero is published only on a clean fetch with no cache to fall back to."""
    cache = load_capture_fixture(cache_path)
    text = fetch_valid_page(source.url, must_contain)
    if text is None:
        items, status, note, raw_path = cache, "stale", \
            f"{len(cache)} from last-good cache — fetch blocked/invalid (stale)", None
    else:
        raw_path = save_raw(source, text)
        try:
            live = parser(source, text)
        except Exception as exc:  # a parser crash must not zero a cache-backed source either
            live, parse_error = [], str(exc)
        else:
            parse_error = None
        if live:
            # Complete clean fetch: live wins, and cache-only rows the source no longer lists age
            # out after CACHE_MISS_LIMIT misses (cancellations/renames) instead of living forever.
            items = age_cache(live, cache)
            save_capture_fixture(cache_path, items)
            status, note = "ok", f"imported {len(live)} upcoming exhibitions ({len(items)} after cache merge)"
        elif cache:
            items = cache  # parsed nothing / parser crashed — serve cache, don't age or overwrite
            reason = f"parser error: {parse_error}" if parse_error else "clean fetch parsed nothing (check parser/shape)"
            status, note = "stale", f"{len(cache)} from cache — {reason}"
        else:
            items = []
            status, note = "ok", "0 — clean fetch, nothing upcoming"
    for item in items:
        upsert_item(conn, source, item)
        ensure_model_enrichment_placeholder(conn, source, item)
    record_run(conn, source, status, note, raw_path)
    return len(items)


def parse_tate(source: Source, text: str) -> list[dict[str, Any]]:
    """Tate Modern / Tate Britain whats-on cards: title in the link aria-label, run in an
    `icon--calendar` <span>. Future-opening exhibitions only; running shows ("Until ..."),
    tours, and talks self-exclude (no parseable opening date)."""
    path = re.search(r"/whats-on/(tate-[a-z]+)", source.url).group(1)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for seg in re.split(rf'(?=<a href="/whats-on/{path}/[a-z0-9-]+" aria-label=)', text):
        link = re.match(rf'<a href="(/whats-on/{path}/[a-z0-9-]+)" aria-label="([^"]+)"', seg)
        date_span = re.search(r'icon--calendar"></i>\s*</div>\s*<span>([^<]+)</span>', seg)
        if not link or not date_span:
            continue
        start, label = tate_opening_date(date_span.group(1))
        url = "https://www.tate.org.uk" + link.group(1)
        if not start or start < today() or start > end_date() or url in seen:
            continue
        seen.add(url)
        items.append({
            "title": normalize_space(html.unescape(link.group(2))),
            "date_start": start.isoformat(),
            "date_label": label,
            "date_precision": "exact",
            "venue_or_platform": source.name,
            "city": "London",
            "source_url": url,
            "external_id": url,
            "description": f"{source.name}, London",
            "importance_score": 14,
        })
    return items


def import_tate(conn: sqlite3.Connection, source: Source) -> int:
    cache = TATE_MODERN_CACHE if "tate-modern" in source.url else TATE_BRITAIN_CACHE
    return import_with_cache(conn, source, cache, parse_tate, must_contain=("icon--calendar",))


def parse_armory_season(text: str) -> list[dict[str, Any]]:
    """Park Avenue Armory current-season page: each event links to /season-events/<slug> with
    its title, and carries a US-format date range ("September 14–26, 2026") nearby. Cut at
    "Previously This Season" to drop past items; keep future openings. Best-effort: the page is
    Cloudflare-gated so this runs only on a non-blocked fetch (else the fixture is used)."""
    cut = text.find("Previously This Season")
    region = text[:cut] if cut > 0 else text
    skip = {"current-season", "2026-season", "event-series", "past-events", "subscriptions",
            "calendar", "tickets", "plan-your-visit", "support", "membership"}
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in re.finditer(r'href="(?:https://www\.armoryonpark\.org)?/season-events/([a-z0-9-]+)/?"(.*?)</a>', region, re.S):
        slug, inner = m.group(1), m.group(2)
        if slug in skip or slug in seen:
            continue
        title = normalize_space(html.unescape(strip_tags(inner)))
        if not title or len(title) < 3:
            continue
        start, label = extract_pac_date(strip_tags(region[m.start():m.start() + 700]))
        if not start or start < today() or start > end_date():
            continue
        seen.add(slug)
        items.append({
            "title": title,
            "date_start": start.isoformat(),
            "date_label": label,
            "date_precision": "exact",
            "venue_or_platform": "Park Avenue Armory",
            "city": "New York",
            "source_url": f"https://www.armoryonpark.org/season-events/{slug}",
            "external_id": f"armory:{slug}",
            "importance_score": 13,
        })
    return items


def armory_fieldwise_merge(cache: list[dict[str, Any]], live: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge the Armory current-season parse into the authoritative cache fieldwise: for a show in
    both, refresh the volatile fields (date, label, precision, URL) from live but KEEP the cache's
    hand-curated discipline category (the live listing doesn't carry it). New live shows are added."""
    by_title = {normalized_dedupe_title(c["title"]): dict(c) for c in cache}
    for lv in live:
        key = normalized_dedupe_title(lv["title"])
        if key in by_title:
            for field in ("date_start", "date_label", "date_precision", "source_url"):
                if lv.get(field):
                    by_title[key][field] = lv[field]
        else:
            by_title[key] = dict(lv)
    return list(by_title.values())


def import_armory(conn: sqlite3.Connection, source: Source) -> int:
    """Park Avenue Armory. One request to the current-season page (no detail-page fan-out, no
    ticketing pages). The site is Cloudflare-gated, so on a 403/429/challenge or an empty parse
    we use the committed fixture as the last-known-good cache — a block degrades freshness, it
    doesn't break the run. A good fetch refreshes the fixture (met_opera-style self-heal)."""
    # The committed fixture is the authoritative last-good cache (it carries the per-discipline
    # categories that come from the page's section markup). A blocked/truncated fetch is a FAILED
    # fetch — serve the cache and flag it stale, never publish empty. A clean fetch only ADDS shows
    # the cache is missing; it never overwrites the cache (so categories are preserved).
    cache = load_capture_fixture(ARMORY_CAPTURE)
    text = fetch_valid_page(source.url, must_contain=("season-events",))
    raw_path = save_raw(source, text) if text else None
    if text is None:
        items, status = cache, "stale"
        note = f"{len(cache)} served from last-good cache — live fetch blocked (data may be stale)"
    else:
        items = armory_fieldwise_merge(cache, parse_armory_season(text))
        added = len(items) - len(cache)
        status = "ok"
        note = f"last-good cache{f' + {added} new from current-season page' if added else ''} ({len(items)})"
    for item in items:
        upsert_item(conn, source, item)
        ensure_model_enrichment_placeholder(conn, source, item)
    record_run(conn, source, status, note, raw_path)
    return len(items)


SERPENTINE_ANNUAL_URL = "https://www.serpentinegalleries.org/whats-on/2026-at-serpentine/"


def parse_serpentine_annual(text: str) -> list[dict[str, Any]]:
    """The annual "2026 at Serpentine" page lists each show as a heading of the form
    "Title (23 September 2026 – January 2027)". An independent source from the paginated What's On
    listing, so a blocked or reordered page 2 can't hide a future exhibition. Future-opening only."""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in re.finditer(
        r"<h[1-4][^>]*>\s*([^()<]+?)\s*\((\d{1,2} [A-Z][a-z]+ 20\d\d[^)<]*)\)\s*</h[1-4]>", text):
        title = normalize_space(html.unescape(m.group(1)))
        start, _ = tate_opening_date(m.group(2))
        key = normalized_dedupe_title(title)
        if not title or not start or start < today() or start > end_date() or key in seen:
            continue
        seen.add(key)
        items.append({
            "title": title,
            "date_start": start.isoformat(),
            "date_label": normalize_space(m.group(2)),
            "date_precision": "exact",
            "venue_or_platform": "Serpentine",
            "city": "London",
            "source_url": SERPENTINE_ANNUAL_URL,
            "external_id": f"serpentine-annual:{key}",
            "description": "Serpentine Galleries, London",
            "importance_score": 14,
        })
    return items


SERPENTINE_PRESS_URL = "https://www.serpentinegalleries.org/about/press/"


def collect_serpentine_press(limit: int = 12) -> tuple[list[dict[str, Any]], bool]:
    """Third independent source: per-exhibition press-release pages (precise run dates, no
    pagination). Returns (items, ok); ok is False only if the press index itself is blocked.
    Each press page is validated, so a blocked one is skipped — never breaks the run."""
    index = fetch_valid_page(SERPENTINE_PRESS_URL, must_contain=("/about/press/",))
    if index is None:
        return [], False
    slugs: list[str] = []
    for slug in re.findall(r"/about/press/([a-z0-9-]+)/", index):
        if slug not in slugs and slug not in {"page", "previous-serpentine-pavilions"}:
            slugs.append(slug)
    items: list[dict[str, Any]] = []
    for slug in slugs[:limit]:
        page = fetch_valid_page(f"{SERPENTINE_PRESS_URL}{slug}/")
        if page is None:
            continue
        meta = MetaParser()
        meta.feed(page)
        title = re.split(r"\s*[-|]\s*Serpentine", meta.meta.get("og:title") or "")[0].strip()
        title = title.title() if title.isupper() else title  # press titles are often ALL CAPS
        run = re.search(r"(\d{1,2} [A-Z][a-z]+(?: \d{4})? ?[–-] ?\d{1,2} [A-Z][a-z]+ \d{4})", strip_tags(page))
        if not title or not run:
            continue
        start, _ = tate_opening_date(run.group(1))
        if not start or start < today() or start > end_date():
            continue
        items.append({
            "title": normalize_space(html.unescape(title)),
            "date_start": start.isoformat(),
            "date_label": normalize_space(run.group(1)),
            "date_precision": "exact",
            "venue_or_platform": "Serpentine",
            "city": "London",
            "source_url": f"{SERPENTINE_PRESS_URL}{slug}/",
            "external_id": f"serpentine-press:{slug}",
            "description": "Serpentine Galleries, London",
            "importance_score": 14,
        })
    return items, True


def import_serpentine(conn: sqlite3.Connection, source: Source, max_pages: int = 8) -> int:
    """Serpentine Galleries (London). The What's On listing is server-rendered teaser cards
    (teaser__pretitle category, teaser__title link, meta__row spans for venue + UK-format date).
    Serpentine mixes categories and ongoing projects across pages, so a future exhibition can
    land on /whats-on/page/2/+. Crawl page by page until a page fails or has no teaser cards.
    Keep future-opening 'Exhibitions' only."""
    base = "https://www.serpentinegalleries.org/whats-on/"
    live_items: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw_path = None
    blocked = False
    for page in range(1, max_pages + 1):
        url = base if page == 1 else f"{base}page/{page}/"
        try:
            text = fetch_text(url)
        except requests.HTTPError as exc:
            # 404 = past the last page (a normal end); other HTTP errors = a failed/blocked fetch.
            if exc.response is not None and exc.response.status_code == 404:
                break
            blocked = True
            break
        except Exception:
            blocked = True
            break
        lowered = text.lower()
        if len(text) < 8000 or "been blocked" in lowered or "attention required" in lowered or "just a moment" in lowered:
            blocked = True  # challenge / truncated shell -> failed fetch, not an empty page
            break
        if page == 1:
            raw_path = save_raw(source, text)
        cards = re.split(r'(?=<section class="teaser )', text)
        if len(cards) <= 1:  # valid page, genuinely no teaser cards -> end of pagination (not blocked)
            break
        for card in cards:
            # Category, venue, and pretitle all wrap nested <a> tags, so strip before matching.
            category = re.search(r"teaser__pretitle(.*?)teaser__title", card, re.S)
            if not category or "exhibition" not in strip_tags(category.group(1)).lower():
                continue
            link = re.search(r'href="(https://www\.serpentinegalleries\.org/whats-on/[a-z0-9-]+/)"', card)
            title = re.search(r'teaser__title[^>]*>\s*(?:<a[^>]*>\s*)?([^<]+)', card)
            rows = [normalize_space(strip_tags(r)) for r in re.findall(r'class="meta__row"[^>]*>(.*?)</span>', card, re.S)]
            rows = [r for r in rows if r]
            date_text = next((r for r in rows if re.search(r"20\d\d", r)), None)
            venue = next((r for r in rows if not re.search(r"20\d\d", r)
                          and r.lower() not in {"free", "sold out", "fully booked"} and not r.startswith("£")),
                         "Serpentine")
            if not link or not title or not date_text:
                continue
            start, label = tate_opening_date(date_text)  # shared UK day-first parser
            target = link.group(1).split("?")[0]
            if not start or start < today() or start > end_date() or target in seen:
                continue
            seen.add(target)
            item = {
                "title": normalize_space(html.unescape(title.group(1))),
                "date_start": start.isoformat(),
                "date_label": label,
                "date_precision": "exact",
                "venue_or_platform": normalize_space(html.unescape(venue)),
                "city": "London",
                "source_url": target,
                "external_id": target,
                "description": "Serpentine Galleries, London",
                "importance_score": 14,
            }
            live_items.append(item)
    # Source diversity (three independent live paths): the paginated listing, the annual programme
    # page, and the per-exhibition press pages. A blocked/reordered/missing one can't hide a future
    # exhibition. Merge all three, then the last-good cache. Listing/annual win titles over press
    # (cleaner casing); press adds precise dates for anything they miss.
    annual = fetch_valid_page(SERPENTINE_ANNUAL_URL, must_contain=("Coming in 2026",))
    press_items, press_ok = collect_serpentine_press()
    live = merge_by_title(merge_by_title(live_items, parse_serpentine_annual(annual) if annual else []), press_items)
    all_blocked = blocked and annual is None and not press_ok
    cache = load_capture_fixture(SERPENTINE_CAPTURE)
    items = merge_by_title(live, cache)  # live wins; cache only fills rows no live source returned
    # Integrity rule: a failed scrape makes data STALE, never empty. Refresh the cache only when a
    # live source returned real data; serve the cache (stale) when every source is blocked/empty.
    if all_blocked or not live:
        status = "stale"
        note = f"{len(items)} from last-good cache — all live sources blocked/empty (stale)"
    else:
        save_capture_fixture(SERPENTINE_CAPTURE, items)
        srcs = (0 if blocked else 1) + (1 if annual else 0) + (1 if press_ok else 0)
        status = "ok"
        note = f"imported {len(live)} from {srcs}/3 live sources ({len(items)} after cache merge)"
    for item in items:
        upsert_item(conn, source, item)
        ensure_model_enrichment_placeholder(conn, source, item)
    record_run(conn, source, status, note, raw_path)
    return len(items)


def parse_va(source: Source, text: str) -> list[dict[str, Any]]:
    """Victoria and Albert Museum (London). The exhibitions listing embeds schema.org microdata
    per card (`<li id="SLUG" data-wo-type="exhibition"><article itemprop="event">` with meta
    name/startDate/endDate), so it's fully scriptable with no detail hydration. Future-opening."""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in re.finditer(
        r'id="([a-z0-9-]+)"[^>]*data-wo-type="exhibition".*?'
        r'itemprop="name" content="([^"]+)"[^<]*'
        r'<meta itemprop="startDate" content="(20\d\d-\d\d-\d\d)"[^<]*'
        r'<meta itemprop="endDate" content="(20\d\d-\d\d-\d\d)"',
        text, re.S,
    ):
        slug, title, start_str, end_str = match.groups()
        title = normalize_space(html.unescape(title))
        try:
            start, end = dt.date.fromisoformat(start_str), dt.date.fromisoformat(end_str)
        except ValueError:
            continue
        if not title or slug in seen or start < today() or start > end_date():
            continue
        seen.add(slug)
        url = f"https://www.vam.ac.uk/exhibitions/{slug}"
        items.append({
            "title": title,
            "date_start": start.isoformat(),
            "date_label": f"{format_us_date(start)} – {format_us_date(end)}",
            "date_precision": "exact",
            "venue_or_platform": "Victoria and Albert Museum",
            "city": "London",
            "source_url": url,
            "external_id": url,
            "description": "V&A, South Kensington, London",
            "importance_score": 13,
        })
    return items


def import_va(conn: sqlite3.Connection, source: Source) -> int:
    return import_with_cache(conn, source, VA_CACHE, parse_va, must_contain=('data-wo-type="exhibition"',))


def parse_flv(source: Source, text: str) -> list[dict[str, Any]]:
    """Fondation Louis Vuitton (Paris). The rest of the site is an Akamai-gated SPA, but the
    English "Coming soon" page (/en/programme/a-venir) server-renders the cards: callout__title
    link + callout__kicker + callout__subtitle ("From DD.MM.YYYY to ...", European day-first).
    Only that public HTML surface — no Akamai-protected APIs. Future-opening exhibitions."""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in re.finditer(
        r'callout__link" href="(https://www\.fondationlouisvuitton\.fr/en/[^"]+)">\s*(.*?)\s*</a>.*?'
        r'callout__kicker">([^<]*)</p>\s*'
        r'<p class="callout__subtitle">From (\d{2})\.(\d{2})\.(20\d\d) to (\d{2})\.(\d{2})\.(20\d\d)',
        text, re.S,
    ):
        url, title, kicker, d1, m1, y1, d2, m2, y2 = match.groups()
        if kicker.strip().lower() != "exhibition":
            continue
        try:
            start, end = dt.date(int(y1), int(m1), int(d1)), dt.date(int(y2), int(m2), int(d2))
        except ValueError:
            continue
        url = url.split("?")[0]
        if url in seen or start < today() or start > end_date():
            continue
        seen.add(url)
        items.append({
            "title": normalize_space(html.unescape(strip_tags(title))),
            "date_start": start.isoformat(),
            "date_label": f"{format_us_date(start)} – {format_us_date(end)}",
            "date_precision": "exact",
            "venue_or_platform": "Fondation Louis Vuitton",
            "city": "Paris",
            "source_url": url,
            "external_id": url,
            "description": "Fondation Louis Vuitton, Paris",
            "importance_score": 15,
        })
    return items


def import_flv(conn: sqlite3.Connection, source: Source) -> int:
    return import_with_cache(conn, source, FLV_CACHE, parse_flv, must_contain=("callout__subtitle",))


def import_lacma(conn: sqlite3.Connection, source: Source) -> int:
    """LACMA (Drupal Views) — listing cards expose title + start/end date fields. Fetchable
    directly (no anti-bot), so fully scriptable. Future-opening only."""
    text = fetch_text(source.url)
    raw_path = save_raw(source, text)
    card = re.compile(
        r'<h2[^>]*>\s*<a href="(/art/exhibition/[^"]+)"[^>]*>([^<]+)</a>\s*</h2>.*?'
        r'field-start-date">\s*<div class="field-content">\s*([^<]+?)\s*</div>.*?'
        r'field-end-date">\s*<div class="field-content">\s*([^<]+?)\s*</div>',
        re.S,
    )
    matched = card.findall(text)
    count = 0
    seen: set[str] = set()
    for href, title_raw, start_str, end_str in matched:
        title = strip_tags(title_raw)
        if not title:
            continue
        start, _ = extract_exhibition_window(f"{normalize_space(start_str)}–{normalize_space(end_str)}")
        if not start or start < today() or start > end_date():  # future-opening only
            continue
        url = urljoin("https://www.lacma.org", href.split("?")[0])
        if url in seen:
            continue
        seen.add(url)
        item = {
            "title": title,
            "date_start": start.isoformat(),
            "date_label": normalize_space(f"{start_str}–{end_str}"),
            "date_precision": "exact",
            "venue_or_platform": "LACMA",
            "city": "Los Angeles",
            "source_url": url,
            "external_id": url,
            "description": "LACMA, Los Angeles",
        }
        upsert_item(conn, source, item)
        ensure_model_enrichment_placeholder(conn, source, item)
        count += 1
    # Shape sanity: matching 0 cards means the page changed, not that there's nothing upcoming
    # (a legit empty still matches current-show cards). Flag it instead of a silent zero.
    if not matched:
        record_run(conn, source, "error", "0 cards matched — page shape changed (check parser)", raw_path)
    else:
        record_run(conn, source, "ok", f"imported {count} upcoming exhibitions ({len(matched)} listed)", raw_path)
    return count


def import_tvmaze(conn: sqlite3.Connection, source: Source, aperture: str) -> int:
    text = fetch_text(source.url)
    raw_path = save_raw(source, text)
    data = json.loads(text)
    count = 0
    start = today()
    stop = end_date()
    principals_cache: dict[Any, list[str]] = {}
    for episode in data:
        airdate = episode.get("airdate")
        if not airdate:
            continue
        try:
            parsed = dt.date.fromisoformat(airdate)
        except ValueError:
            continue
        if parsed < start or parsed > stop:
            continue
        show = episode.get("_embedded", {}).get("show") or episode.get("show") or {}
        if not is_relevant_tv_episode(episode, show, aperture):
            continue
        show_name = show.get("name") or "Unknown show"
        episode_name = episode.get("name")
        season = episode.get("season")
        number = episode.get("number")
        if number == 1 and season and season > 1:
            title = f"{show_name}: Season {season} premiere"
        elif number == 1 and season == 1:
            title = f"{show_name}: Series premiere"
        else:
            title = show_name if not episode_name else f"{show_name}: {episode_name}"
        network = tv_channel_name(show)
        score = tv_importance_score(episode, show)
        notes = [
            f"show_type={show.get('type') or 'unknown'}",
            f"season={episode.get('season')}",
            f"episode={episode.get('number')}",
        ]
        item = {
            "title": title,
            "date_start": airdate,
            "date_precision": "exact",
            "date_label": airdate,
            "venue_or_platform": network,
            "source_url": episode.get("url") or show.get("url"),
            "external_id": str(episode.get("id")),
            "description": "; ".join(notes) + ". " + (normalize_space(re.sub("<[^>]+>", "", episode.get("summary") or "")) or ""),
            "importance_score": score,
        }
        # Surface principals for the stronger signals only, to bound API calls.
        if score >= 40 and show.get("id") is not None:
            item["people"] = tvmaze_principals(show.get("id"), principals_cache)
        upsert_item(conn, source, item)
        ensure_model_enrichment_placeholder(conn, source, item)
        count += 1
    record_run(conn, source, "ok", f"imported {count} episodes", raw_path)
    return count


# IBDB (the official Internet Broadway Database) is the forward-looking Broadway source:
# its /shows page carries the announced "Current & Upcoming" slate, including productions
# that aren't yet on sale on Broadway.org. We scope to that section (the page also embeds an
# "Opening Nights in History" block we must skip), hydrate each production for its opening
# date, keep future openings in the horizon, and let dedupe_theatre fold overlaps into the
# canonical Broadway.org rows.
IBDB_DETAIL_BASE = "https://www.ibdb.com/broadway-production/"


def parse_ibdb_listing(text: str) -> list[str]:
    """Production slugs from IBDB /shows, scoped to the 'Current & Upcoming' section only."""
    start = text.find("Current & Upcoming")
    end = text.find("Opening Nights in History")
    segment = text[start:end] if (start >= 0 and end > start) else (text[start:] if start >= 0 else text)
    slugs: list[str] = []
    seen: set[str] = set()
    for slug in re.findall(r"/broadway-production/([a-z0-9\-]+)", segment):
        if slug not in seen:
            seen.add(slug)
            slugs.append(slug)
    return slugs


def ibdb_title(detail: str) -> str | None:
    m = re.search(r"<title>([^<|]+?)\s+[–-]\s+Broadway", detail)
    return normalize_space(html.unescape(m.group(1))) if m else None


def ibdb_show_type(detail: str) -> str:
    m = re.search(r"<title>[^<|]+?\s+[–-]\s+Broadway\s+(Play|Musical)", detail)
    return m.group(1) if m else "production"


def ibdb_opening_date(detail: str) -> tuple[str, str, str] | None:
    """Parse IBDB's Opening Date into (iso_start, precision, label). Handles a firm
    'Mon DD, YYYY' and a vague 'Mon YYYY'; returns None for TBD / year-only / missing."""
    m = re.search(r'xt-lable">\s*Opening Date\s*</div>\s*<div class="xt-main-title">\s*([^<]+)', detail, re.S)
    if not m:
        return None
    value = normalize_space(html.unescape(m.group(1)))
    firm = re.match(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})$", value)
    if firm:
        month = MONTH_NUMBERS.get(firm.group(1).lower())
        if month:
            day = dt.date(int(firm.group(3)), month, int(firm.group(2)))
            return day.isoformat(), "exact", format_us_date(day)
    vague = re.match(r"([A-Za-z]+)\.?\s+(\d{4})$", value)
    if vague:
        month = MONTH_NUMBERS.get(vague.group(1).lower())
        if month:
            day = dt.date(int(vague.group(2)), month, 1)
            return day.isoformat(), "month", f"{vague.group(1).title()} {vague.group(2)}"
    return None


def import_ibdb(conn: sqlite3.Connection, source: Source) -> int:
    """Forward-looking Broadway via IBDB. Net-new announced productions land here; overlaps
    with Broadway.org are dropped by dedupe_theatre (Broadway.org stays canonical)."""
    text = fetch_text(source.url)
    raw_path = save_raw(source, text)
    count = 0
    seen: set[str] = set()
    for slug in parse_ibdb_listing(text):
        # Pre-filter obvious carried-over long-runs by slug so we don't hydrate ~20 needless
        # detail pages (and trip IBDB's rate limiter).
        slug_title = re.sub(r"-\d+$", "", slug).replace("-", " ").strip()
        if is_carried_over_broadway_title(slug_title):
            continue
        url = IBDB_DETAIL_BASE + slug
        try:
            detail = fetch_text(url)
        except Exception:
            continue
        time.sleep(0.3)  # be polite — IBDB rate-limits rapid sequential fetches
        title = ibdb_title(detail)
        if not title or is_carried_over_broadway_title(title):
            continue
        parsed = ibdb_opening_date(detail)
        if not parsed:
            continue  # TBD / year-only — no firm planning date
        date_start, precision, label = parsed
        opening = dt.date.fromisoformat(date_start)
        if opening < today() or opening > end_date():
            continue
        key = normalized_dedupe_title(title)
        if key in seen:
            continue
        seen.add(key)
        item = {
            "title": title,
            "category": "theatre",
            "date_start": date_start,
            "date_precision": precision,
            "date_label": label,
            "venue_or_platform": "Broadway",
            "city": "New York",
            "source_url": url,
            "external_id": f"ibdb:{slug}",
            "description": f"Broadway {ibdb_show_type(detail)}",
            "importance_score": 18,
            "people": extract_theatre_principals(detail) or None,
        }
        upsert_item(conn, source, item)
        ensure_model_enrichment_placeholder(conn, source, item)
        count += 1
    record_run(conn, source, "ok", f"imported {count} upcoming Broadway productions", raw_path)
    return count


# Editorial cap for film: keep the top releases by TMDb popularity and credit every one.
# The long popularity tail is where the non-US-relevant noise lives (regional digital-only
# dumps, foreign-language titles with no real US release), so the cap doubles as a quality
# gate. We also require an actual US theatrical/limited release (with_release_type 2|3) so a
# culture desk sees what's reviewable here, not a worldwide release-date firehose.
# The global popularity pass keeps the buzzy near-term slate; a per-month pass then walks the
# whole horizon so far-out months keep their top releases instead of being crowded out (a
# single popularity.desc cut clusters near-term and drops, e.g., Dec-2026 prestige titles).
TMDB_GLOBAL_FILMS = 50
TMDB_PER_MONTH = 8


def _month_starts(start: dt.date, end: dt.date) -> list[dt.date]:
    """First-of-month dates from start's month through end's month, inclusive."""
    months: list[dt.date] = []
    cursor = start.replace(day=1)
    while cursor <= end:
        months.append(cursor)
        cursor = (cursor.replace(day=28) + dt.timedelta(days=7)).replace(day=1)
    return months


def import_tmdb(conn: sqlite3.Connection, source: Source) -> int:
    token = os.environ.get(source.requires_env or "")
    if not token:
        record_run(conn, source, "skipped", f"missing {source.requires_env}")
        return 0
    seen: set[str] = set()
    raw_holder: list[Path | None] = [None]

    def harvest(window: dict[str, str], budget: int) -> None:
        """Discover US theatrical/limited releases in a date window, most-popular first, and
        upsert up to `budget` not-yet-seen films (each credit-enriched)."""
        page = 1
        added = 0
        while page <= 5 and added < budget:
            params = {
                "region": "US",
                "with_release_type": "2|3",  # 2 = limited theatrical, 3 = theatrical (US)
                "sort_by": "popularity.desc",
                "include_adult": "false",
                "page": page,
                **window,
            }
            text = fetch_text(source.url, params=params, headers={"Authorization": f"Bearer {token}"})
            if raw_holder[0] is None:
                raw_holder[0] = save_raw(source, text)
            data = json.loads(text)
            results = data.get("results", [])
            for movie in results:
                if added >= budget:
                    break
                title = movie.get("title")
                external_id = str(movie.get("id"))
                if not title or external_id in seen:
                    continue
                seen.add(external_id)
                release_date = movie.get("release_date")
                item = {
                    "title": title,
                    "date_start": release_date,
                    "date_precision": "exact" if release_date else "unknown",
                    "date_label": release_date,
                    "source_url": f"https://www.themoviedb.org/movie/{movie.get('id')}",
                    "external_id": external_id,
                    "description": movie.get("overview"),
                    "importance_score": int(movie.get("popularity") or 0),
                }
                # Every kept film is credit-enriched (director/writer/cast) — no popularity cutoff.
                if movie.get("id"):
                    item["people"] = tmdb_principals(movie["id"], token)
                upsert_item(conn, source, item)
                ensure_model_enrichment_placeholder(conn, source, item)
                added += 1
            if not results or page >= int(data.get("total_pages", page)):
                break
            page += 1

    horizon_end = end_date()
    # Pass 1: globally most-popular upcoming films (near-term slate).
    harvest({"release_date.gte": today().isoformat(), "release_date.lte": horizon_end.isoformat()},
            TMDB_GLOBAL_FILMS)
    # Pass 2: top releases month by month across the horizon (dedup is by external_id via `seen`).
    for month in _month_starts(today(), horizon_end):
        nxt = (month.replace(day=28) + dt.timedelta(days=7)).replace(day=1)
        lo = max(month, today())
        hi = min(nxt - dt.timedelta(days=1), horizon_end)
        if lo > hi:
            continue
        harvest({"release_date.gte": lo.isoformat(), "release_date.lte": hi.isoformat()},
                TMDB_PER_MONTH)

    count = len(seen)
    record_run(conn, source, "ok",
               f"imported {count} films (popularity slate + per-month horizon, all credited)",
               raw_holder[0])
    return count


def save_capture_fixture(path: Path, items: list[dict[str, Any]]) -> None:
    """Cache a source's parsed items so a blocked CI run can still show them.

    Used by the sources that fetch cleanly from a normal IP but get blocked
    (429 / JS shell / empty parse) from datacenter/CI IPs: the Met museum and Met
    Opera both refresh their committed fixture on any good fetch.
    """
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(
        {"capturedAt": today().isoformat(), "items": items}, indent=2, ensure_ascii=False))


def load_capture_fixture(path: Path) -> list[dict[str, Any]]:
    """Fall back to a committed fixture, keeping only still-future (or undated) items."""
    if not path.exists():
        return []
    keep: list[dict[str, Any]] = []
    for item in json.loads(path.read_text()).get("items", []):
        start = item.get("date_start")
        if not start:
            keep.append(item)
            continue
        try:
            day = dt.date.fromisoformat(start)
        except ValueError:
            keep.append(item)
            continue
        if today() <= day <= end_date():
            keep.append(item)
    return keep


# Hand-maintained capture fixtures: source id -> committed JSON of normalized items. For
# venues with no scriptable path (Cloudflare/JS/non-English). Refresh each season by hand.
CAPTURE_FIXTURE_SOURCES = {
    "npg_london": NPG_CAPTURE,
    "grand_palais": GRAND_PALAIS_CAPTURE,
    "centre_pompidou": POMPIDOU_CAPTURE,
    "mam_paris": MAM_CAPTURE,
}


def import_html_source(conn: sqlite3.Connection, source: Source) -> int:
    # Capture-only sources: the live page is bot-protected with no fetchable data, so we
    # parse a browser-captured fixture (refreshed via Claude-in-Chrome) instead of the network.
    if source.id == "frick":
        items = parse_frick_capture(source)
        for item in items:
            upsert_item(conn, source, item)
            ensure_model_enrichment_placeholder(conn, source, item)
        record_run(conn, source, "ok", f"parsed {len(items)} from browser capture")
        return len(items)
    if source.id in CAPTURE_FIXTURE_SOURCES:
        # Bot-walled / JS / non-English venues with no scriptable path (Park Avenue Armory,
        # Tate Modern, …): hand-maintained capture fixtures refreshed each season. Horizon
        # filtering drops events whose opening has already passed.
        items = load_capture_fixture(CAPTURE_FIXTURE_SOURCES[source.id])
        for item in items:
            upsert_item(conn, source, item)
            ensure_model_enrichment_placeholder(conn, source, item)
        record_run(conn, source, "ok", f"loaded {len(items)} from committed fixture")
        return len(items)
    raw_path = None
    # Fixture-backed sources tolerate ANY fetch failure (a 403/429/challenge, whether requests
    # raises or curl rejects a non-2xx) and fall back to their committed fixture below — a failed
    # fetch must serve stale data, never go empty.
    # Fetch-tolerant sources keep a committed last-good cache; a 403/429/challenge serves it
    # rather than zeroing. The museum-framework sources are included (they 429 from CI IPs like
    # the Met), so e.g. a Brooklyn Museum rate-limit no longer wipes the venue.
    fixture_backed = {"moma_exhibitions", "met_exhibitions", "met_opera_2026_27"} | set(MUSEUMS)
    try:
        text = fetch_text(source.url)
        raw_path = save_raw(source, text)
    except (requests.HTTPError, RuntimeError):
        if source.id not in fixture_backed:
            raise
        text = ""
    if source.id in MUSEUMS:
        # Distinguish a real empty (cards present, none upcoming — legitimate) from a failed
        # fetch / broken page shape (no cards). Only the latter falls back to the cache, so a
        # legitimate "nothing upcoming" doesn't resurrect stale shows.
        if not text:
            cards, items = 0, []
        elif MUSEUMS[source.id].get("json"):
            items = parse_museum_json(source, text)
            cards = len(items)
        else:
            listing = parse_museum_listing(source, text)
            cards = len(listing)
            items = hydrate_museum_dates(conn, source, listing)
        cache_path = museum_cache_path(source.id)
        if items:
            save_capture_fixture(cache_path, items)
            status, note = "ok", f"parsed {len(items)} upcoming exhibitions"
        elif cards == 0:  # fetch failed or page shape broke — serve last-good cache
            items = load_capture_fixture(cache_path)
            status = "stale" if items else "ok"
            note = (f"{len(items)} from last-good cache — fetch failed/empty page (stale)"
                    if items else "0 — fetch failed/empty and no cache")
        else:  # clean fetch, cards present, none upcoming — a legitimate empty
            status, note = "ok", f"0 upcoming ({cards} current shows listed)"
        for item in items:
            upsert_item(conn, source, item)
            ensure_model_enrichment_placeholder(conn, source, item)
        record_run(conn, source, status, note, raw_path)
        return len(items)
    parser_by_source = {
        "broadway_org": parse_broadway_org,
        "playbill_broadway": parse_playbill,
        "playbill_offbroadway": parse_playbill_offbroadway,
        "bam_programs": parse_bam,
        "aoty_upcoming": parse_aoty_upcoming,
        "metacritic_albums": parse_metacritic_albums,
        "met_exhibitions": parse_met_exhibitions,
        "moma_exhibitions": parse_moma_exhibitions,
        "met_opera_2026_27": parse_met_opera,
        "nycb_seasons": parse_nycb,
    }
    parser = parser_by_source.get(source.id, parse_links)
    items = parser(source, text) if text else []
    used_moma_capture = False
    if source.id == "moma_exhibitions":
        # Fixture-backed source of truth. The live scraper is behind a flag (MOMA_LIVE, default
        # off) because moma.org WAFs our hosts with a 403. The live parse may override the
        # committed fixture ONLY when the fetched document proves it's the real index (both
        # section headings + an in-section exhibition link); a 403 / JS shell / empty parse
        # leaves the fixture untouched — never replaced with an empty or unverified result.
        live_items = parse_moma_exhibitions(source, text) \
            if (moma_live_enabled() and text and moma_document_valid(text)) else []
        if live_items:
            save_capture_fixture(MOMA_CAPTURE_LINKS, live_items)  # refresh from a verified fetch
            items = live_items
        else:
            items = parse_moma_capture(source)
            used_moma_capture = True
    used_met_opera_capture = False
    used_met_capture = False
    if source.id == "met_exhibitions":
        # The Met fetches fine from a normal IP but 429s CI/datacenter IPs. Refresh the
        # committed fixture on a good fetch; fall back to it when the live parse is empty.
        if items:
            save_capture_fixture(MET_CAPTURE, items)
        else:
            items = load_capture_fixture(MET_CAPTURE)
            used_met_capture = True
    if source.id == "met_opera_2026_27":
        # metopera.org serves CI/datacenter IPs a shell that parses to 0 links, so the live
        # page lost the whole season. Same treatment as the Met museum: refresh the committed
        # fixture on a good fetch, fall back to it when the live parse is empty.
        if items:
            hydrate_met_opera_credits(source, items)  # director/conductor/lead singers
            save_capture_fixture(MET_OPERA_CAPTURE, items)
        else:
            items = load_capture_fixture(MET_OPERA_CAPTURE)
            used_met_opera_capture = True
    if source.id == "broadway_org":
        hydrate_broadway_org_dates(conn, source, items)
        # A show whose opening date is already past is a carried-over run (e.g. Chess
        # opened Nov 2025), not an upcoming production. Exclude by date, so we don't
        # depend on a hand-maintained title list or a render-time guardrail.
        items = [item for item in items if not broadway_already_open(item)]
    for item in items:
        upsert_item(conn, source, item)
        ensure_model_enrichment_placeholder(conn, source, item)
    enrich_detail_pages(conn, source, items)
    # A fixture fallback means the live page failed/empty — report it as stale, not ok, so the
    # "Source runs" section reflects real degradation.
    degraded = used_moma_capture or used_met_opera_capture or used_met_capture
    source_note = ""
    if used_moma_capture:
        source_note = (" from committed fixture (live fetch disabled)" if not moma_live_enabled()
                       else " from committed fixture (live 403/unverified — fixture kept)")
    elif used_met_opera_capture:
        source_note = " from committed fixture fallback (live empty)"
    elif used_met_capture:
        source_note = " from committed fixture fallback (live 429/empty)"
    record_run(conn, source, "stale" if degraded else "ok",
               f"parsed {len(items)} candidate links{source_note}", raw_path)
    return len(items)


def broadway_already_open(item: dict[str, Any]) -> bool:
    start = item.get("date_start")
    if not start:
        return False
    try:
        return dt.date.fromisoformat(start) < today()
    except ValueError:
        return False


NAME_STOPWORDS = {
    "Cast", "Creative", "Email", "Signup", "Set", "Designer", "Costume", "Lighting",
    "Sound", "Production", "Conductor", "Composer", "Select", "Tickets", "Buy", "The",
    "A", "An", "This", "Music", "Book", "Written", "Directed", "Choreographed", "Starring",
    "New", "Season", "Calendar", "Synopsis", "Run", "Time", "Learn", "More", "Photo",
    "Setting", "Libretto", "Performed", "Performance", "Opera", "Act", "Acts", "Sung",
    "World", "Premiere", "Based", "After", "Adapted", "Original", "Story", "Translated",
    "English", "Italian", "German", "French", "Featuring", "With", "And", "In", "Revival",
    "For", "Also", "Plus", "Tickets", "Visit", "See",
    # Production-credit field labels that follow a name in stripped text and otherwise bleed
    # into it (e.g. "Whitney White Press", "Justin Martin Scenic", "Chelsey Arce Casting").
    "Press", "Scenic", "Scenery", "Casting", "Projection", "Projections", "Orchestrations",
    "Orchestration", "General", "Management", "Manager", "Representative", "Associate",
    "Supervisor", "Supervising", "Hair", "Wig", "Wigs", "Makeup", "Design", "Arrangements",
    "Incidental", "Additional", "Fight", "Dialect", "Vocal", "Dramaturg", "Direction",
    "Produced", "Producer", "Producers", "Presented", "Co",
}


def capture_name_after(text: str, label: str) -> str | None:
    """Capture a 1-3 word proper name following a role label, stopping at section words."""
    # Include accented Latin letters so e.g. "Leoš Janáček", "Nézet-Séguin" are not truncated.
    letter = r"A-Za-zÀ-ÿĀ-ſ"  # Latin-1 + Latin Extended-A (Janáček, Dvořák, …)
    match = re.search(
        rf"{label}[ :]+([A-ZÀ-ÞĀ-ſ][{letter}.’'\-]+(?:\s+[A-ZÀ-ÞĀ-ſ][{letter}.’'\-]+){{0,3}})",
        text,
    )
    if not match:
        return None
    out: list[str] = []
    for token in match.group(1).split():
        if token in NAME_STOPWORDS:
            break
        ends_sentence = token.endswith(".") and len(token) > 2  # not an initial like "J."
        out.append(token.rstrip(".") if ends_sentence else token)
        if ends_sentence or len(out) >= 3:
            break
    return " ".join(out) if out else None


def extract_theatre_principals(html_text: str) -> list[dict[str, str]]:
    """Role-labeled creative team + lead from a Playbill / Broadway.org detail page."""
    people: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(name: str | None, role: str) -> None:
        if name and name not in seen:
            people.append({"name": name, "role": role})
            seen.add(name)

    text = strip_tags(html_text)
    for label, role in (
        ("Written by", "Writer"), ("Book by", "Writer"), ("Music by", "Composer"),
        ("Lyrics by", "Lyricist"), ("Directed by", "Director"),
        ("Choreographed by", "Choreographer"), ("Starring", "Cast"),
    ):
        add(capture_name_after(text, label), role)
    # Playbill exposes the playwright as a /person/ link rather than a "Written by" label.
    has_writer = any(p["role"] in {"Writer", "Playwright"} for p in people)
    for name in re.findall(r'/person/[^"]+"[^>]*>([^<]{2,40})</a>', html_text):
        name = normalize_space(html.unescape(name))
        if name and name not in seen and not has_writer:
            add(name, "Playwright")
            has_writer = True
            break
    return people[:8]


def hydrate_broadway_org_dates(conn: sqlite3.Connection, source: Source, items: list[dict[str, Any]]) -> None:
    for item in items:
        url = item.get("source_url")
        external_id = item.get("external_id")
        if not url or not external_id:
            continue
        try:
            html_text = fetch_text(url)
            raw_path = save_detail(source, external_id, html_text)
            upsert_detail(conn, source, item, html_text, raw_path)
        except Exception:
            continue
        apply_broadway_date_fields(item, extract_broadway_date_fields(html_text))
        principals = extract_theatre_principals(html_text)
        if principals:
            item["people"] = principals
        # Name the actual theatre (like Off-Broadway does) instead of the generic "Broadway".
        theatre = re.search(r'aside-block-theatre.*?<h5>\s*([^<]+?)\s*</h5>', html_text, re.S)
        if theatre:
            item["venue_or_platform"] = normalize_space(html.unescape(theatre.group(1)))


def enrich_detail_pages(conn: sqlite3.Connection, source: Source, items: list[dict[str, Any]], limit: int = 25) -> None:
    if source.id not in {
        "playbill_broadway",
        "playbill_offbroadway",
        "met_exhibitions",
        "moma_exhibitions",
        "met_opera_2026_27",
        "nycb_seasons",
    }:
        return
    for item in items[:limit]:
        url = item.get("source_url")
        external_id = item.get("external_id")
        if not url or not external_id:
            continue
        try:
            html_text = fetch_text(url)
            raw_path = save_detail(source, external_id, html_text)
            upsert_detail(conn, source, item, html_text, raw_path)
        except Exception:
            continue
        # Pull principals from the detail page and update the already-inserted row. Met Opera
        # credits are set up-front by hydrate_met_opera_credits (so the capture fixture carries
        # them and CI doesn't need a live fetch); here we only refresh the stored detail text.
        if source.id == "playbill_offbroadway":
            principals = extract_theatre_principals(html_text)
        else:
            principals = []
        if principals:
            conn.execute(
                "update items set people_json = ? where source_id = ? and external_id = ?",
                (json.dumps(principals), source.id, external_id),
            )
            conn.commit()


CREDIT_ROLE_PHRASE = {
    "Playwright": "By",
    "Composer": "Music by",
    "Director": "Directed by",
    "Writer": "Written by",
    "Conductor": "Cond.",
    "Choreographer": "Choreography by",
    "Lyricist": "Lyrics by",
    "Creator": "Created by",
    "Showrunner": "Showrunner",
    "Cast": "With",
}
CREDIT_ROLE_ORDER = [
    "Playwright", "Composer", "Director", "Writer", "Conductor",
    "Choreographer", "Lyricist", "Creator", "Showrunner", "Cast",
]


def format_credits(people_json: str | None) -> str:
    """Render role-labeled people into a credit line, e.g.
    "Directed by Christopher Nolan · Written by … · With Matt Damon, Tom Holland".
    The Artist role is suppressed because it's already the album's title."""
    try:
        people = json.loads(people_json) if people_json else []
    except (ValueError, TypeError):
        people = []
    by_role: dict[str, list[str]] = {}
    for entry in people:
        if isinstance(entry, dict):
            name, role = entry.get("name"), entry.get("role") or "Cast"
        else:  # tolerate legacy flat-string rows
            name, role = entry, "Cast"
        if not name or role == "Artist":
            continue
        by_role.setdefault(role, []).append(name)
    segments = []
    for role in CREDIT_ROLE_ORDER:
        names = by_role.get(role)
        if not names:
            continue
        cap = 3 if role == "Cast" else 2
        shown = ", ".join(names[:cap])
        if len(names) > cap:
            shown += f" +{len(names) - cap}"
        segments.append(f"{CREDIT_ROLE_PHRASE[role]} {shown}")
    return " · ".join(segments)


CATEGORY_DISPLAY = {
    "film": "Film", "tv": "Television", "theatre": "Theatre", "art": "Art",
    "music": "Music", "opera": "Opera", "ballet": "Dance",
}
CATEGORY_DISPLAY_ORDER = ["film", "tv", "theatre", "art", "music", "opera", "ballet"]
# Music splits into two editorial lanes in the render: live concerts vs. album releases.
# PAC's music-genre events are live concerts, so they render in the Concerts lane too.
CONCERT_MUSIC_SOURCES = {"nyphil_concerts", "carnegie_hall", "pac_nyc", "the_shed", "armory"}


def render_html(conn: sqlite3.Connection) -> None:
    dated = conn.execute(
        """
        select category, title, date_start, date_precision,
               coalesce(date_label, date_start, 'TBA') as date_label,
               source_name, source_id, venue_or_platform, source_url,
               importance_score, people_json
        from items
        where date_start is not null and date(date_start) between date(?) and date(?)
        order by date_start
        """,
        (today().isoformat(), end_date().isoformat()),
    ).fetchall()
    horizon_rows = conn.execute(
        """
        select category, title, coalesce(date_label, 'TBA') as date_label, date_start,
               source_name, source_id, venue_or_platform, source_url,
               importance_score, people_json
        from items
        where date_start is null
          and date_label is not null
          and lower(date_label) not like '%unknown%'
          and not (category = 'art' and (date_precision = 'unknown'
                or lower(date_label) like '%ongoing%' or lower(date_label) like '%through%'))
          and not (category = 'theatre' and (date_precision = 'unknown'
                or lower(date_label) like '%closes%' or lower(date_label) like '%closing%'))
          and not (category = 'music' and date_precision = 'unknown')
        order by importance_score desc, category, title
        """
    ).fetchall()
    # Latest run per source, all of them — not the last N rows (which silently dropped
    # sources once the count grew past the limit).
    run_rows = conn.execute(
        "select source_name, status, message from source_runs "
        "where id in (select max(id) from source_runs group by source_id) "
        "order by source_name"
    ).fetchall()

    # Relevance = percentile within source (hidden; orders entries inside each category).
    source_scores: dict[str, list[int]] = {}
    for row in dated:
        source_scores.setdefault(row["source_id"], []).append(row["importance_score"] or 0)

    def relevance(source_id: str, value: int) -> float:
        values = source_scores.get(source_id, [])
        if len(values) <= 1 or min(values) == max(values):
            return 50.0
        return sum(1 for v in values if v < value) / (len(values) - 1) * 100

    def date_display(label: str | None) -> str:
        if label and re.fullmatch(r"20\d\d-\d\d-\d\d", label):
            return format_us_date(dt.date.fromisoformat(label))
        return label or "TBA"

    def compact_date(label: str | None) -> str:
        return re.sub(r",?\s*20\d\d", "", date_display(label)).strip()

    def entry_rows(rows: list[sqlite3.Row]) -> str:
        out = []
        for row in rows:
            url = row["source_url"] or "#"
            out.append(
                "<tr>"
                f"<td class=\"date\">{html.escape(date_display(row['date_label']))}</td>"
                f"<td><a href=\"{html.escape(url)}\">{html.escape(row['title'])}</a></td>"
                f"<td class=\"credits\">{html.escape(format_credits(row['people_json']))}</td>"
                f"<td>{html.escape(row['venue_or_platform'] or '')}</td>"
                "</tr>"
            )
        return "".join(out)

    def column_list(rows: list[sqlite3.Row], compact: bool = True) -> str:
        # A compact two-column (date + title) list — far tighter than a near-empty table.
        # Used for music (the artist is already in the title) and for horizon ballet. compact
        # drops the year; horizon entries keep it so "Fall 2026" vs "Winter 2027" stays clear.
        items = []
        for row in rows:
            url = row["source_url"] or "#"
            venue = row["venue_or_platform"]
            extra = f" <span class=\"v\">· {html.escape(venue)}</span>" if venue else ""
            shown_date = compact_date(row['date_label']) if compact else date_display(row['date_label'])
            items.append(
                "<div class=\"entry\">"
                f"<span class=\"d\">{html.escape(shown_date)}</span>"
                f"<span class=\"body\"><a href=\"{html.escape(url)}\">{html.escape(row['title'])}</a>{extra}</span>"
                "</div>"
            )
        return f"<div class=\"cols2\">{''.join(items)}</div>"

    def category_blocks(by_cat: dict[str, list[sqlite3.Row]],
                        compact_cats: frozenset = frozenset()) -> str:
        blocks = []
        ordered = [c for c in CATEGORY_DISPLAY_ORDER if c in by_cat] + [
            c for c in by_cat if c not in CATEGORY_DISPLAY_ORDER
        ]
        for cat in ordered:
            rows = sorted(
                by_cat[cat],
                key=lambda r: (
                    -relevance(r["source_id"], r["importance_score"] or 0),
                    r["date_start"] or r["date_label"] or "",
                    r["title"],
                ),
            )
            head = f"<h3>{html.escape(CATEGORY_DISPLAY.get(cat, cat.title()))}</h3>"
            if cat == "music":
                # Concerts and album releases are different editorial signals — separate them.
                concerts = [r for r in rows if r["source_id"] in CONCERT_MUSIC_SOURCES]
                albums = [r for r in rows if r["source_id"] not in CONCERT_MUSIC_SOURCES]
                parts = []
                if concerts:
                    parts.append("<h3>Music · Concerts</h3>" + column_list(concerts))
                if albums:
                    parts.append("<h3>Music · Albums</h3>" + column_list(albums))
                blocks.append("".join(parts))
            elif cat in compact_cats:
                # Short label+title entries (e.g. horizon ballet seasons) read better as a
                # two-column list than a sparse four-column table; keep the full season label.
                blocks.append(head + column_list(rows, compact=False))
            else:
                blocks.append(
                    head
                    + "<table><thead><tr><th>Date</th><th>Title</th><th>Credits</th>"
                    "<th>Venue / Platform</th></tr></thead>"
                    f"<tbody>{entry_rows(rows)}</tbody></table>"
                )
        return "".join(blocks)

    def calendar_view(rows: list[sqlite3.Row]) -> str:
        # Day-by-day: each opening/premiere day is a heading with that day's entries from every
        # category beneath it. Album releases (the Metacritic feed — low signal, often many in a
        # day) are run into one comma-separated line rather than a line each.
        def is_album(r: sqlite3.Row) -> bool:
            return r["category"] == "music" and r["source_id"] not in CONCERT_MUSIC_SOURCES

        def cal_entry(r: sqlite3.Row) -> str:
            cat = CATEGORY_DISPLAY.get(r["category"], r["category"].title())
            url = r["source_url"] or "#"
            meta = " · ".join(x for x in (r["venue_or_platform"], format_credits(r["people_json"])) if x)
            meta_html = f" <span class=\"cal-meta\">· {html.escape(meta)}</span>" if meta else ""
            return (
                "<div class=\"cal-entry\">"
                f"<span class=\"cal-cat\">{html.escape(cat)}</span>"
                f"<span class=\"cal-body\"><a href=\"{html.escape(url)}\">{html.escape(r['title'])}</a>{meta_html}</span>"
                "</div>"
            )

        def render_group(group: list[sqlite3.Row]) -> str:
            albums = sorted((r for r in group if is_album(r)), key=lambda r: r["title"].lower())
            others = sorted((r for r in group if not is_album(r)), key=lambda r: (
                CATEGORY_DISPLAY_ORDER.index(r["category"]) if r["category"] in CATEGORY_DISPLAY_ORDER else 99,
                -relevance(r["source_id"], r["importance_score"] or 0),
                r["title"],
            ))
            rendered = [cal_entry(r) for r in others]
            if albums:
                links = ", ".join(
                    f"<a href=\"{html.escape(r['source_url'] or '#')}\">{html.escape(r['title'])}</a>"
                    for r in albums
                )
                rendered.append(
                    "<div class=\"cal-entry\"><span class=\"cal-cat\">Albums</span>"
                    f"<span class=\"cal-body\">{links}</span></div>"
                )
            return "".join(rendered)

        # Only day-precise rows get a calendar day. Month/season/TBA rows (e.g. Metacritic's
        # "Dec 2026" stored as 2026-12-01) would imply a false exact date, so they go to a
        # per-month "date to be confirmed" block instead.
        exact_precision = {"exact", "exact_or_range"}
        by_day: dict[str, list[sqlite3.Row]] = {}
        coarse_by_month: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            if row["date_precision"] in exact_precision:
                by_day.setdefault(row["date_start"], []).append(row)
            else:
                coarse_by_month.setdefault(row["date_start"][:7], []).append(row)
        blocks: list[tuple[str, str]] = []
        for day, group in by_day.items():
            heading = dt.date.fromisoformat(day).strftime("%A, %B %-d, %Y")
            blocks.append((day, f"<div class=\"cal-day\"><div class=\"cal-date\">{heading}</div>{render_group(group)}</div>"))
        for mkey, group in coarse_by_month.items():
            heading = dt.date(int(mkey[:4]), int(mkey[5:7]), 1).strftime("%B %Y") + " · date to be confirmed"
            # sort after that month's day-blocks
            blocks.append((f"{mkey}-99", f"<div class=\"cal-day\"><div class=\"cal-date\">{heading}</div>{render_group(group)}</div>"))
        return "".join(html_block for _, html_block in sorted(blocks))

    months: dict[str, dict[str, list[sqlite3.Row]]] = {}
    for row in dated:
        months.setdefault(row["date_start"][:7], {}).setdefault(row["category"], []).append(row)
    month_sections = []
    for mkey in sorted(months):
        label = dt.date(int(mkey[:4]), int(mkey[5:7]), 1).strftime("%B %Y")
        month_sections.append(f"<h2>{label}</h2>" + category_blocks(months[mkey]))
    calendar_html = calendar_view(dated)

    horizon_by_cat: dict[str, list[sqlite3.Row]] = {}
    for row in horizon_rows:
        horizon_by_cat.setdefault(row["category"], []).append(row)
    horizon_html = ""
    if horizon_by_cat:
        horizon_html = (
            "<h2>On the horizon <span class=\"sub\">(announced, dates still vague)</span></h2>"
            + category_blocks(horizon_by_cat, compact_cats=frozenset({"ballet"}))
        )

    run_html = "".join(
        f"<li>{html.escape(r['source_name'])}: {html.escape(r['status'])} — {html.escape(r['message'] or '')}</li>"
        for r in run_rows
    )
    # Source health is tucked into the "Source runs" disclosure at the foot of the page: the
    # summary surfaces a stale count so degradation is discoverable without shouting at the top.
    degraded = [r["source_name"] for r in run_rows if r["status"] in ("stale", "skipped", "error")]
    runs_summary = "Source runs"
    freshness_html = ""
    if degraded:
        shown = ", ".join(html.escape(n) for n in degraded[:8]) + (f" +{len(degraded) - 8}" if len(degraded) > 8 else "")
        runs_summary = f"Source runs — {len(degraded)} of {len(run_rows)} stale or unavailable"
        freshness_html = f"<p class=\"freshness\">Served from last-good cache where possible: {shown}.</p>"

    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Cultural Calendar</title>
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='%232a2722'/><rect x='6' y='9' width='20' height='17' rx='2' fill='%23f6f4ee'/><rect x='6' y='9' width='20' height='5' fill='%233a5a66'/><rect x='10' y='6' width='2.4' height='5' rx='1' fill='%23f6f4ee'/><rect x='19.6' y='6' width='2.4' height='5' rx='1' fill='%23f6f4ee'/><g fill='%232a2722'><rect x='9' y='17' width='3' height='3' rx='.6'/><rect x='14.5' y='17' width='3' height='3' rx='.6'/><rect x='20' y='17' width='3' height='3' rx='.6'/><rect x='9' y='22' width='3' height='3' rx='.6'/><rect x='14.5' y='22' width='3' height='3' rx='.6'/></g></svg>">
  <style>
    html {{ background: #efece3; }}
    body {{ font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", Palatino, Georgia, serif; margin: 0; color: #322f29; }}
    .sheet {{ max-width: 1080px; margin: 0 auto; padding: 40px 48px 56px; min-height: 100vh;
      border-left: 1px solid #e6e2d8; border-right: 1px solid #e6e2d8;
      background: radial-gradient(circle at 20% 6%, rgba(252,251,247,.45), transparent 55%),
        radial-gradient(circle at 85% 96%, rgba(223,219,208,.1), transparent 55%), #f6f4ee; }}
    h1 {{ font-size: 42px; font-weight: 400; letter-spacing: -.01em; color: #2a2722; margin: 0 0 4px; }}
    h2 {{ font-size: 23px; font-weight: 400; color: #2a2722; margin: 40px 0 4px; padding-bottom: 5px; border-bottom: 1px solid rgba(90,84,66,.3); }}
    h3 {{ font-size: 12px; font-weight: 400; text-transform: uppercase; letter-spacing: .14em; color: #9a7c44; margin: 22px 0 2px; }}
    .sub {{ color: #8c8675; font-weight: 400; font-size: 14px; }}
    p.lede {{ color: #6d685d; font-size: 14px; margin: 0 0 8px; }}
    details.runs p.freshness {{ color: #8a5a2b; font-size: 12px; margin: 4px 0 6px; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 6px; }}
    th, td {{ border-bottom: 1px solid rgba(110,100,75,.12); padding: 7px 10px; text-align: left; vertical-align: top; font-size: 15px; }}
    th {{ font-size: 11px; font-weight: 400; text-transform: uppercase; letter-spacing: .08em; color: #8c8675; border-bottom: 1px solid rgba(90,84,66,.28); }}
    td.date {{ white-space: nowrap; color: #6d685d; font-style: italic; width: 120px; }}
    td.credits {{ color: #4a4640; }}
    .cols2 {{ column-count: 2; column-gap: 36px; margin-top: 8px; }}
    .cols2 .entry {{ display: flex; gap: 10px; break-inside: avoid; padding: 5px 0; font-size: 15px; border-bottom: 1px solid rgba(110,100,75,.1); }}
    .cols2 .d {{ flex: 0 0 104px; color: #6d685d; font-style: italic; white-space: nowrap; }}
    .cols2 .body {{ flex: 1; min-width: 0; }}
    .cols2 .v {{ color: #8c8675; font-size: 12.5px; }}
    a {{ color: #3a5a66; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    /* View toggle (pure CSS): Editorial (month -> category) vs Calendar (day-by-day). */
    input.vtoggle {{ position: absolute; opacity: 0; pointer-events: none; }}
    .viewtoggle {{ margin: 18px 0 4px; display: inline-flex; border: 1px solid #cfc8b6; border-radius: 7px; overflow: hidden; }}
    .viewtoggle label {{ cursor: pointer; padding: 5px 16px; font-size: 13px; color: #6d685d; background: #f1eee4; }}
    .viewtoggle label + label {{ border-left: 1px solid #cfc8b6; }}
    #view-editorial:checked ~ .viewtoggle label[for="view-editorial"],
    #view-calendar:checked ~ .viewtoggle label[for="view-calendar"] {{ background: #3a5a66; color: #f6f4ee; }}
    .view {{ display: none; }}
    #view-editorial:checked ~ .view-editorial {{ display: block; }}
    #view-calendar:checked ~ .view-calendar {{ display: block; }}
    .cal-day {{ margin-top: 20px; break-inside: avoid; }}
    .cal-date {{ font-size: 16px; color: #2a2722; margin-bottom: 2px; padding-bottom: 5px; border-bottom: 1px solid rgba(90,84,66,.3); }}
    .cal-entry {{ display: flex; gap: 12px; padding: 6px 0; font-size: 15px; border-bottom: 1px solid rgba(110,100,75,.1); }}
    .cal-cat {{ flex: 0 0 92px; color: #9a7c44; text-transform: uppercase; font-size: 11px; letter-spacing: .1em; padding-top: 3px; }}
    .cal-body {{ flex: 1; min-width: 0; }}
    .cal-meta {{ color: #8c8675; font-size: 12.5px; }}
    details.runs {{ margin-top: 32px; color: #8c8675; font-size: 12px; }}
    details.runs li {{ list-style: none; }}
  </style>
</head>
<body>
  <main class="sheet">
  <h1>Cultural Calendar</h1>
  <p class="lede">Significant releases, openings, premieres, exhibitions, and performances on the editorial horizon. Generated {dt.datetime.now().strftime("%Y-%m-%d %H:%M")}.</p>
  <input id="view-editorial" class="vtoggle" type="radio" name="view" checked>
  <input id="view-calendar" class="vtoggle" type="radio" name="view">
  <div class="viewtoggle"><label for="view-editorial">Editorial</label><label for="view-calendar">Calendar</label></div>
  <div class="view view-editorial">{''.join(month_sections)}</div>
  <div class="view view-calendar">{calendar_html}</div>
  {horizon_html}
  <details class="runs"><summary>{runs_summary}</summary>{freshness_html}<ul>{run_html}</ul></details>
  </main>
</body>
</html>
"""
    HTML_PATH.write_text(page)


def normalized_dedupe_title(title: str) -> str:
    normalized = normalize_space(title).lower()
    # Drop a trailing subtitle so the same show collapses across sources, e.g. a venue feed's
    # "Giulia" and Playbill's "Giulia: The Poison Queen of Palermo" -> "giulia".
    return re.sub(r"\s*:\s.*$", "", normalized)


def dedupe_theatre(conn: sqlite3.Connection) -> None:
    """Drop the same theatre production arriving from more than one source.

    Broadway.org is canonical for opening dates (SKILL.md), so it wins, then Playbill
    Broadway, then the Off-Broadway aggregator, then the single-venue feeds. Playbill carries
    fuller titles + credits, so it outranks the venue feeds (BAM, PAC, The Shed, Armory) when
    they list the same production. Keyed on normalized title + start date, so revivals on
    different dates stay distinct.
    """
    priority = {
        "broadway_org": 0,
        "ibdb": 1,
        "playbill_broadway": 2,
        "playbill_offbroadway": 3,
        "bam_programs": 4,
        "pac_nyc": 5,
        "the_shed": 6,
        "armory": 7,
    }
    rows = conn.execute(
        "select id, source_id, title, date_start from items where category = 'theatre'"
    ).fetchall()
    best: dict[tuple[str, str | None], tuple[int, int]] = {}
    drop: list[int] = []
    for row in rows:
        key = (normalized_dedupe_title(row["title"]), row["date_start"])
        rank = priority.get(row["source_id"], 9)
        if key in best:
            keep_rank, keep_id = best[key]
            loser = row["id"] if rank >= keep_rank else keep_id
            if rank < keep_rank:
                best[key] = (rank, row["id"])
            drop.append(loser)
        else:
            best[key] = (rank, row["id"])
    for item_id in drop:
        conn.execute("delete from items where id = ?", (item_id,))
    conn.commit()
