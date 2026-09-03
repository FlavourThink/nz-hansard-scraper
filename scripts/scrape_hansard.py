#!/usr/bin/env python3
"""
NZ Hansard scraper — daily + historical (HTML era, Feb 2003 → present).

Writes:
  data/hansard_index.json
  data/days/YYYY-MM-DD.json

Usage:
  python scripts/scrape_hansard.py --date 2024-08-14
  python scripts/scrape_hansard.py --from 2003-02-01 --to 2026-08-16 --delay 2.5
  python scripts/scrape_hansard.py --recent 14
  python scripts/scrape_hansard.py --reindex
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DAYS = DATA / "days"
INDEX_PATH = DATA / "hansard_index.json"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Official site domains (HTML Hansard from ~2003)
BASE = "https://www.parliament.nz"
# Legacy combined daily pattern (still used by some scrapers / archives)
COMBINED_TMPL = (
    "https://www.parliament.nz/en/pb/hansard-debates/rhr/combined/HansD_{d1}_{d2}"
)
# Document-style URLs observed on the modern site
DOC_SEARCH = (
    "https://www.parliament.nz/en/pb/hansard-debates/rhr/"
)

USER_AGENT = (
    "NZHansardScraper/1.0 (+https://github.com/YOUR_USER/nz-hansard-scraper; "
    "research; polite; contact via repo issues)"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-NZ,en;q=0.9",
}

# Sitting weekdays only (House does not sit every day)
# We still try every date; non-sitting days simply 404 / empty.

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ---------------------------------------------------------------------------
# Index helpers
# ---------------------------------------------------------------------------

def load_index() -> dict[str, Any]:
    if INDEX_PATH.exists():
        return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    return {
        "updated_at": None,
        "coverage_note": (
            "HTML Hansard on parliament.nz is reliably available from ~Feb 2003. "
            "Winston Peters first elected 25 Nov 1978; pre-2003 needs PDF/OCR."
        ),
        "days": {},  # date_str -> {url, scraped_at, path, title, status}
    }


def save_index(index: dict[str, Any]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    index["updated_at"] = datetime.now(timezone.utc).isoformat()
    INDEX_PATH.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def day_path(d: date) -> Path:
    return DAYS / f"{d.isoformat()}.json"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def fetch(url: str, delay: float = 2.0) -> requests.Response | None:
    """GET with polite delay. Returns None on 404 / hard failure."""
    time.sleep(delay)
    try:
        r = SESSION.get(url, timeout=60, allow_redirects=True)
        if r.status_code == 404:
            return None
        # Bot walls sometimes return 200 with captcha HTML
        if "captcha" in r.text.lower() or "perfdrive" in r.text.lower():
            print(f"  ! bot protection / captcha at {url}", file=sys.stderr)
            return None
        r.raise_for_status()
        return r
    except requests.RequestException as e:
        print(f"  ! request error {url}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# URL discovery for a calendar date
# ---------------------------------------------------------------------------

def candidate_urls(d: date) -> list[str]:
    """Generate likely Hansard URLs for a sitting day."""
    ymd = d.strftime("%Y%m%d")
    ymd_dash = d.isoformat()
    urls = [
        # Classic combined daily (DATE1 == DATE2)
        COMBINED_TMPL.format(d1=ymd, d2=ymd),
        # Sometimes multi-day combined uses adjacent dates — try same only first
    ]
    # Modern document-style guesses (site structure has shifted over years)
    urls += [
        f"{BASE}/en/pb/hansard-debates/rhr/document/HansD_{ymd}_{ymd}",
        f"{BASE}/en/pb/hansard-debates/rhr/document/HansS_{ymd}_000000000",
        f"https://hansard.parliament.nz/Hansard/{ymd_dash}",
    ]
    return urls


def find_hansard_page(d: date, delay: float) -> tuple[str, str] | None:
    """
    Try candidate URLs; return (final_url, html) for the first that looks like Hansard.
    """
    for url in candidate_urls(d):
        print(f"  try {url}")
        r = fetch(url, delay=delay)
        if r is None:
            continue
        html = r.text
        # Heuristic: real Hansard pages are long and mention debate/question markers
        if len(html) < 2000:
            continue
        lower = html.lower()
        if any(
            x in lower
            for x in (
                "hansard",
                "oral question",
                "speaker",
                "nzpd",
                "debate",
                "member for",
            )
        ):
            return r.url, html
    return None


# ---------------------------------------------------------------------------
# Parse HTML → structured day record
# ---------------------------------------------------------------------------

SPEAKER_RE = re.compile(
    r"^(?:Hon\.?\s+|Rt\s+Hon\.?\s+|Dr\s+|Mr\s+|Mrs\s+|Ms\s+|Miss\s+)?"
    r"([A-Z][a-zA-Z' -]+?)(?:\s*\(|:|\s{2,})",
)


def clean_text(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_hansard_html(html: str, d: date, url: str) -> dict[str, Any]:
    """
    Best-effort parse. Parliament HTML structure has changed over time;
    we keep both structured sections and a full plain-text fallback.
    """
    soup = BeautifulSoup(html, "lxml")

    # Remove scripts/styles/nav noise
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    title = None
    if soup.title and soup.title.string:
        title = clean_text(soup.title.string)
    h1 = soup.find("h1")
    if h1:
        title = clean_text(h1.get_text(" ", strip=True))

    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    # Walk common heading + paragraph patterns
    body = soup.find("main") or soup.find("article") or soup.body or soup
    for el in body.find_all(["h1", "h2", "h3", "h4", "p", "div"], recursive=True):
        name = el.name
        text = clean_text(el.get_text(" ", strip=True))
        if not text or len(text) < 2:
            continue

        if name in ("h1", "h2", "h3", "h4"):
            current = {"heading": text, "items": []}
            sections.append(current)
            continue

        if current is None:
            current = {"heading": "Body", "items": []}
            sections.append(current)

        item: dict[str, Any] = {"type": "text", "text": text}

        # Speaker detection (e.g. "Hon CHRIS HIPKINS: ...")
        if ":" in text[:80]:
            left, _, right = text.partition(":")
            left = left.strip()
            if 3 < len(left) < 80 and left[0].isupper():
                item = {
                    "type": "speech",
                    "speaker": left,
                    "text": right.strip() or text,
                }

        # Question markers
        low = text.lower()
        if low.startswith("question no") or "oral question" in low[:40]:
            item["type"] = "question_header"

        current["items"].append(item)

    # Plain text fallback for search / bulk NLP
    plain = clean_text(body.get_text("\n", strip=True))

    return {
        "date": d.isoformat(),
        "url": url,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "title": title,
        "sections": sections,
        "plain_text": plain,
        "raw_text_length": len(plain),
        "parser": "scrape_hansard.py/v1",
    }


# ---------------------------------------------------------------------------
# Core scrape actions
# ---------------------------------------------------------------------------

def scrape_day(d: date, delay: float, force: bool = False) -> dict[str, Any] | None:
    DAYS.mkdir(parents=True, exist_ok=True)
    out = day_path(d)

    if out.exists() and not force:
        print(f"= skip existing {d.isoformat()}")
        return json.loads(out.read_text(encoding="utf-8"))

    print(f"> scrape {d.isoformat()}")
    found = find_hansard_page(d, delay=delay)
    if not found:
        print(f"  no Hansard page for {d.isoformat()}")
        return None

    url, html = found
    record = parse_hansard_html(html, d, url)
    out.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"  wrote {out} ({record['raw_text_length']} chars)")
    return record


def update_index_for_day(index: dict[str, Any], record: dict[str, Any] | None, d: date) -> None:
    key = d.isoformat()
    if record is None:
        index["days"][key] = {
            "status": "missing",
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }
        return
    index["days"][key] = {
        "status": "ok",
        "url": record.get("url"),
        "title": record.get("title"),
        "scraped_at": record.get("scraped_at"),
        "path": f"data/days/{key}.json",
        "raw_text_length": record.get("raw_text_length"),
    }


def daterange(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def reindex_from_files(index: dict[str, Any]) -> dict[str, Any]:
    DAYS.mkdir(parents=True, exist_ok=True)
    for p in sorted(DAYS.glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            d = date.fromisoformat(p.stem)
            update_index_for_day(index, rec, d)
        except Exception as e:
            print(f"  reindex fail {p}: {e}", file=sys.stderr)
    return index


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="NZ Hansard scraper (HTML era)")
    ap.add_argument("--date", help="Single day YYYY-MM-DD")
    ap.add_argument("--from", dest="date_from", help="Start date YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", help="End date YYYY-MM-DD (default: today)")
    ap.add_argument(
        "--recent",
        type=int,
        metavar="N",
        help="Scrape last N calendar days (incremental daily mode)",
    )
    ap.add_argument("--delay", type=float, default=2.0, help="Seconds between requests")
    ap.add_argument("--force", action="store_true", help="Re-scrape even if file exists")
    ap.add_argument("--reindex", action="store_true", help="Rebuild index from data/days")
    args = ap.parse_args()

    index = load_index()

    if args.reindex:
        index = reindex_from_files(index)
        save_index(index)
        print(f"Index rebuilt: {len(index['days'])} days")
        return 0

    dates: list[date] = []

    if args.date:
        dates = [date.fromisoformat(args.date)]
    elif args.recent:
        today = date.today()
        dates = list(daterange(today - timedelta(days=args.recent - 1), today))
    elif args.date_from:
        start = date.fromisoformat(args.date_from)
        end = date.fromisoformat(args.date_to) if args.date_to else date.today()
        if start < date(2003, 2, 1):
            print(
                "NOTE: HTML Hansard starts ~2003-02. Pre-2003 needs PDF/OCR. "
                "Clamping start to 2003-02-01.",
                file=sys.stderr,
            )
            start = date(2003, 2, 1)
        dates = list(daterange(start, end))
    else:
        ap.print_help()
        print(
            "\nExamples:\n"
            "  python scripts/scrape_hansard.py --recent 14\n"
            "  python scripts/scrape_hansard.py --from 2003-02-01 --to 2003-12-31\n"
            "  python scripts/scrape_hansard.py --date 2024-08-14\n",
            file=sys.stderr,
        )
        return 1

    ok = 0
    missing = 0
    for d in dates:
        # Skip weekends lightly (House almost never sits Sat/Sun) unless forced
        if d.weekday() >= 5 and not args.force:
            # Still try Fridays/odd sittings; only auto-skip pure weekend unless --force
            pass

        rec = scrape_day(d, delay=args.delay, force=args.force)
        update_index_for_day(index, rec, d)
        if rec:
            ok += 1
        else:
            missing += 1
        # Checkpoint index often so long backfills are resumable
        if (ok + missing) % 5 == 0:
            save_index(index)

    save_index(index)
    print(f"\nDone. ok={ok} missing={missing} index_days={len(index['days'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
