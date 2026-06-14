#!/usr/bin/env python3
"""
Toy Cultural Calendar importer.

This is intentionally small and dependency-light. It tests whether public APIs
and official pages can feed a normalized calendar without deciding the final app.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

import subprocess

import requests


# Migrated to the cultural_calendar package (behavior-preserving re-org); re-exported here
# so this module stays runnable during the migration.
from cultural_calendar.core.config import *  # noqa: F401,F403
from cultural_calendar.core.config import ROOT, DATA_DIR, RAW_DIR, DETAIL_DIR, DB_PATH, SOURCES_PATH, HTML_PATH, MOMA_CAPTURE_LINKS, CARNEGIE_CAPTURE, FRICK_CAPTURE, MONTH_PATTERN, MONTH_RE, MONTH_NUMBERS, Source, today, end_date, load_sources
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
    try:
        response = requests.get(url, params=params, headers=base_headers, timeout=30)
        response.raise_for_status()
        return response.text
    except requests.HTTPError as exc:
        # Some sources (e.g. Metacritic) fingerprint the TLS client and 403 urllib3 while
        # serving curl normally. Retry once via curl, which presents a browser-like TLS.
        if exc.response is not None and exc.response.status_code == 403:
            return fetch_with_curl(url, params, base_headers)
        raise


def fetch_with_curl(url: str, params: dict[str, Any] | None, headers: dict[str, str]) -> str:
    full_url = url
    if params:
        full_url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
    command = ["curl", "-sS", "--compressed", "--max-time", "30", full_url]
    for key, value in headers.items():
        command += ["-H", f"{key}: {value}"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=40)
    if result.returncode != 0:
        raise RuntimeError(f"curl fetch failed for {url}: {result.stderr[:200]}")
    return result.stdout


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


def planning_bucket(label: str | None) -> str | None:
    if not label:
        return "Undated / TBA"
    label_lower = label.lower()
    if "fall 2026" in label_lower or "autumn 2026" in label_lower:
        return "Undated fall 2026 events"
    if "summer 2026" in label_lower:
        return "Undated summer 2026 events"
    if "winter 2026" in label_lower:
        return "Undated winter 2026 events"
    if "spring 2026" in label_lower:
        return "Undated spring 2026 events"
    if re.fullmatch(r"2026", label.strip()):
        return "Undated 2026 events"
    return None


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


def parse_moma_capture(source: Source, limit: int = 80) -> list[dict[str, Any]]:
    if not MOMA_CAPTURE_LINKS.exists():
        return []
    data = json.loads(MOMA_CAPTURE_LINKS.read_text())
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
    parser = LinkTextParser(source.url)
    parser.feed(text)
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


def titleize_slug(slug: str) -> str:
    small = {"in", "the", "of", "and", "a", "an", "for", "to", "with"}
    words = slug.replace("-", " ").split()
    return " ".join(
        word if (word in small and index > 0) else word.capitalize()
        for index, word in enumerate(words)
    )


def parse_carnegie_capture(source: Source, limit: int = 200) -> list[dict[str, Any]]:
    """Parse the browser-captured Carnegie Hall fixture (Stern/Perelman + Zankel, Carnegie's
    own programming; see the fixture note for the Algolia capture method). Performers that
    merely repeat the title are suppressed."""
    if not CARNEGIE_CAPTURE.exists():
        return []
    data = json.loads(CARNEGIE_CAPTURE.read_text())
    items: list[dict[str, Any]] = []
    for perf in data.get("performances", []):
        try:
            start = dt.date.fromisoformat(perf["date"])
        except (ValueError, KeyError):
            continue
        if start < today() or start > end_date():
            continue
        title = normalize_space(perf.get("title", ""))
        if not title:
            continue
        title_lower = title.lower()
        people = [
            {"name": p, "role": "Performer"}
            for p in perf.get("performers", [])
            if p and p.lower() not in title_lower
        ]
        hall = perf.get("venue", "")
        items.append(
            {
                "title": title,
                "category": "music",
                "date_start": start.isoformat(),
                "date_label": format_us_date(start),
                "date_precision": "exact",
                "venue_or_platform": "Carnegie Hall",
                "city": "New York",
                "source_url": "https://www.carnegiehall.org/events",
                "external_id": f"carnegie:{perf['date']}:{title_lower[:50]}",
                "people": people,
                "description": f"Carnegie Hall — {hall}" if hall else "Carnegie Hall",
                "importance_score": 16,
            }
        )
        if len(items) >= limit:
            break
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
        if not start or start < today() or start > end_date():
            continue
        item["date_start"] = start.isoformat()
        if item.get("date_label") and re.search(r"\bongoing\b", item["date_label"], re.I):
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
                "venue_or_platform": "Met Opera",
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
        for member in data.get("crew", []):
            if member.get("job") in {"Screenplay", "Writer", "Story", "Author"}:
                add(member.get("name"), "Writer")
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
    count = 0
    seen: set[str] = set()
    for href, title_raw, start_str, end_str in card.findall(text):
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
    record_run(conn, source, "ok", f"imported {count} upcoming exhibitions", raw_path)
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


def import_tmdb(conn: sqlite3.Connection, source: Source) -> int:
    token = os.environ.get(source.requires_env or "")
    if not token:
        record_run(conn, source, "skipped", f"missing {source.requires_env}")
        return 0
    count = 0
    page = 1
    while page <= 5:
        params = {
            "region": "US",
            "primary_release_date.gte": today().isoformat(),
            "primary_release_date.lte": end_date().isoformat(),
            "sort_by": "popularity.desc",
            "page": page,
            "include_adult": "false",
        }
        text = fetch_text(source.url, params=params, headers={"Authorization": f"Bearer {token}"})
        if page == 1:
            raw_path = save_raw(source, text)
        data = json.loads(text)
        for movie in data.get("results", []):
            release_date = movie.get("release_date")
            item = {
                "title": movie.get("title"),
                "date_start": release_date,
                "date_precision": "exact" if release_date else "unknown",
                "date_label": release_date,
                "source_url": f"https://www.themoviedb.org/movie/{movie.get('id')}",
                "external_id": str(movie.get("id")),
                "description": movie.get("overview"),
                "importance_score": int(movie.get("popularity") or 0),
            }
            if item["title"]:
                # Credits cost one call per film; cap at the most popular to bound runtime.
                if count < 60 and movie.get("id"):
                    item["people"] = tmdb_principals(movie["id"], token)
                upsert_item(conn, source, item)
                ensure_model_enrichment_placeholder(conn, source, item)
                count += 1
        if page >= int(data.get("total_pages", page)):
            break
        page += 1
    record_run(conn, source, "ok", f"imported {count} movies", raw_path if count else None)
    return count


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
    raw_path = None
    try:
        text = fetch_text(source.url)
        raw_path = save_raw(source, text)
    except requests.HTTPError:
        if source.id != "moma_exhibitions":
            raise
        text = ""
    if source.id in MUSEUMS:
        if MUSEUMS[source.id].get("json"):
            items = parse_museum_json(source, text)
        else:
            items = hydrate_museum_dates(conn, source, parse_museum_listing(source, text))
        for item in items:
            upsert_item(conn, source, item)
            ensure_model_enrichment_placeholder(conn, source, item)
        record_run(conn, source, "ok", f"parsed {len(items)} upcoming exhibitions", raw_path)
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
    if source.id == "moma_exhibitions" and not items:
        # The live page may now return a 200 JS shell (instead of a 403) that parses to
        # nothing; fall back to the browser-capture fixture whenever the live parse is empty.
        items = parse_moma_capture(source)
        used_moma_capture = True
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
    source_note = " from browser capture fallback" if used_moma_capture else ""
    record_run(conn, source, "ok", f"parsed {len(items)} candidate links{source_note}", raw_path)
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


def extract_opera_principals(html_text: str) -> list[dict[str, str]]:
    """Composer, production director, and conductor from a Met Opera production page."""
    text = strip_tags(html_text)
    people: list[dict[str, str]] = []
    seen: set[str] = set()
    for label, role in (("Composer", "Composer"), ("Production", "Director"), ("Conductor", "Conductor")):
        name = capture_name_after(text, label)
        if name and name not in seen:
            people.append({"name": name, "role": role})
            seen.add(name)
    return people


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
        # Pull principals from the detail page and update the already-inserted row.
        if source.id == "playbill_offbroadway":
            principals = extract_theatre_principals(html_text)
        elif source.id == "met_opera_2026_27":
            principals = extract_opera_principals(html_text)
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
CONCERT_MUSIC_SOURCES = {"nyphil_concerts", "carnegie_hall"}


def render_html(conn: sqlite3.Connection) -> None:
    dated = conn.execute(
        """
        select category, title, date_start,
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
    run_rows = conn.execute(
        "select source_name, status, message from source_runs order by id desc limit 16"
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

    def music_columns(rows: list[sqlite3.Row]) -> str:
        # Music entries are mostly date + title (album artist is already in the title), so
        # a two-column list is far more compact than a near-empty table.
        items = []
        for row in rows:
            url = row["source_url"] or "#"
            venue = row["venue_or_platform"]
            extra = f" <span class=\"v\">· {html.escape(venue)}</span>" if venue else ""
            items.append(
                "<div class=\"entry\">"
                f"<span class=\"d\">{html.escape(compact_date(row['date_label']))}</span>"
                f"<span class=\"body\"><a href=\"{html.escape(url)}\">{html.escape(row['title'])}</a>{extra}</span>"
                "</div>"
            )
        return f"<div class=\"cols2\">{''.join(items)}</div>"

    def category_blocks(by_cat: dict[str, list[sqlite3.Row]]) -> str:
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
                    parts.append("<h3>Music · Concerts</h3>" + music_columns(concerts))
                if albums:
                    parts.append("<h3>Music · Albums</h3>" + music_columns(albums))
                blocks.append("".join(parts))
            else:
                blocks.append(
                    head
                    + "<table><thead><tr><th>Date</th><th>Title</th><th>Credits</th>"
                    "<th>Venue / Platform</th></tr></thead>"
                    f"<tbody>{entry_rows(rows)}</tbody></table>"
                )
        return "".join(blocks)

    months: dict[str, dict[str, list[sqlite3.Row]]] = {}
    for row in dated:
        months.setdefault(row["date_start"][:7], {}).setdefault(row["category"], []).append(row)
    month_sections = []
    for mkey in sorted(months):
        label = dt.date(int(mkey[:4]), int(mkey[5:7]), 1).strftime("%B %Y")
        month_sections.append(f"<h2>{label}</h2>" + category_blocks(months[mkey]))

    horizon_by_cat: dict[str, list[sqlite3.Row]] = {}
    for row in horizon_rows:
        horizon_by_cat.setdefault(row["category"], []).append(row)
    horizon_html = ""
    if horizon_by_cat:
        horizon_html = (
            "<h2>On the horizon <span class=\"sub\">(announced, dates still vague)</span></h2>"
            + category_blocks(horizon_by_cat)
        )

    run_html = "".join(
        f"<li>{html.escape(r['source_name'])}: {html.escape(r['status'])} — {html.escape(r['message'] or '')}</li>"
        for r in run_rows
    )

    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Cultural Calendar</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #1a1a1a; max-width: 1100px; }}
    h1 {{ font-size: 28px; margin-bottom: 2px; }}
    h2 {{ font-size: 22px; margin: 40px 0 4px; border-bottom: 2px solid #111; padding-bottom: 4px; }}
    h3 {{ font-size: 13px; text-transform: uppercase; letter-spacing: .05em; color: #666; margin: 22px 0 0; }}
    .sub {{ color: #888; font-weight: normal; font-size: 14px; }}
    p {{ color: #555; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 6px; }}
    th, td {{ border-bottom: 1px solid #ececec; padding: 7px 10px; text-align: left; vertical-align: top; font-size: 14px; }}
    th {{ font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: #999; border-bottom: 1px solid #ccc; }}
    td.date {{ white-space: nowrap; color: #444; width: 120px; }}
    td.credits {{ color: #333; }}
    .cols2 {{ column-count: 2; column-gap: 36px; margin-top: 8px; }}
    .cols2 .entry {{ display: flex; gap: 10px; break-inside: avoid; padding: 4px 0; font-size: 14px; border-bottom: 1px solid #f0f0f0; }}
    .cols2 .d {{ flex: 0 0 96px; color: #999; white-space: nowrap; }}
    .cols2 .body {{ flex: 1; min-width: 0; }}
    .cols2 .v {{ color: #999; font-size: 12px; }}
    a {{ color: #14506e; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    details.runs {{ margin-top: 28px; color: #999; font-size: 12px; }}
    details.runs li {{ list-style: none; }}
  </style>
</head>
<body>
  <h1>Cultural Calendar</h1>
  <p>Upcoming culturally significant releases, openings, premieres, exhibitions, and performances through 2026. Generated {dt.datetime.now().strftime("%Y-%m-%d %H:%M")}.</p>
  {''.join(month_sections)}
  {horizon_html}
  <details class="runs"><summary>Source runs</summary><ul>{run_html}</ul></details>
</body>
</html>
"""
    HTML_PATH.write_text(page)


def normalized_dedupe_title(title: str) -> str:
    normalized = normalize_space(title).lower()
    return re.sub(r"\s*:\s*(?:a new musical|the musical).*$", "", normalized)


def dedupe_theatre(conn: sqlite3.Connection) -> None:
    """Drop the same theatre production arriving from more than one source.

    Broadway.org is canonical for opening dates (SKILL.md), so it wins, then Playbill
    Broadway, then the Off-Broadway / BAM feeds. Keyed on normalized title + start date
    so revivals on different dates are kept as distinct rows.
    """
    priority = {
        "broadway_org": 0,
        "playbill_broadway": 1,
        "playbill_offbroadway": 2,
        "bam_programs": 3,
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
