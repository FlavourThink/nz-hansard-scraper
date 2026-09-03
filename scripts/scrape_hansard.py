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
  python scripts/scrape_hansard.py --recent 14 --backfill-days 40
  python scripts/scrape_hansard.py --reindex

--backfill-days N  Walk backward from the saved cursor toward 2003-02-01.
                   Checks up to N weekdays per run, then stops and saves
                   the cursor so tomorrow continues. That is how history
                   fills slowly without one giant job.
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
from io import BytesIO
from pypdf import PdfReader

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

PDF_DAILY = "https://hansard.parliament.nz/api/resources/daily/related/{d}/{d}-daily.pdf"

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


def fetch_daily_pdf(d: date, delay: float) -> tuple[str, bytes] | None:
    """Official daily Hansard PDF. 200 + PDF = sitting day. 404 = House did not sit."""
    url = PDF_DAILY.format(d=d.isoformat())
    print(f"  try {url}")
    time.sleep(max(0.0, delay))
    try:
        r = SESSION.get(url, timeout=60)
    except requests.RequestException as e:
        print(f"  request error: {e}")
        return None
    ctype = (r.headers.get("content-type") or "").lower()
    if r.status_code == 404:
        return None
    if r.status_code != 200 or "pdf" not in ctype:
        print(f"  skip {r.status_code} {ctype[:40]}")
        return None
    if not r.content.startswith(b"%PDF"):
        print("  body was not a PDF")
        return None
    return url, r.content


def pdf_to_text(blob: bytes) -> str:
    reader = PdfReader(BytesIO(blob))
    parts = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return "\n".join(parts)


SPEAKER_RE = re.compile(
    r"^(?P<name>[A-ZĀĒĪŌŪ][A-ZĀĒĪŌŪa-zāēīōū\-''\. ]{2,80})"
    r"(?:\s+\((?P<role>[^)]{0,80})\))?"
    r"(?:\s+\((?P<time>\d{1,2}:\d{2})\))?\s*:\s*(?P<rest>.*)$"
)


def parse_plain_text(plain: str, d: date, url: str) -> dict[str, Any]:
    lines = [ln.strip() for ln in plain.splitlines()]
    title = next((ln for ln in lines if ln and "HOUSE OF REPRESENTATIVES" in ln.upper()), "")
    if not title:
        title = f"Hansard {d.isoformat()}"
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    heading_guess = re.compile(r"^[A-ZĀĒĪŌŪ][A-ZĀĒĪŌŪ /,&\-']{8,}$")
    for ln in lines:
        if not ln:
            continue
        if heading_guess.match(ln) and "PAGE " not in ln:
            current = {"heading": ln.title() if ln.isupper() else ln, "items": []}
            sections.append(current)
            continue
        if current is None:
            current = {"heading": "Body", "items": []}
            sections.append(current)
        m = SPEAKER_RE.match(ln)
        if m and len(m.group("name").split()) <= 8:
            current["items"].append({
                "type": "speech",
                "speaker": m.group("name").strip(),
                "role": (m.group("role") or "").strip(),
                "time": (m.group("time") or "").strip(),
                "text": (m.group("rest") or "").strip(),
            })
        else:
            current["items"].append({"type": "text", "text": ln})
    return {
        "date": d.isoformat(),
        "url": url,
        "source": "daily-pdf",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "title": title,
        "sections": sections,
        "plain_text": plain,
        "raw_text_length": len(plain),
        "parser": "scrape_hansard.py/v1.2-pdf",
    }


def scrape_day(d: date, delay: float, force: bool = False) -> dict[str, Any] | None:
    DAYS.mkdir(parents=True, exist_ok=True)
    out = day_path(d)

    if out.exists() and not force:
        print(f"= skip existing {d.isoformat()}")
        return json.loads(out.read_text(encoding="utf-8"))

    print(f"> scrape {d.isoformat()}")
    found = fetch_daily_pdf(d, delay=delay)
    if not found:
        print(f"  no sitting PDF for {d.isoformat()}")
        return None

    url, blob = found
    plain = pdf_to_text(blob)
    if len(plain.strip()) < 80:
        print("  PDF had almost no text")
        return None
    record = parse_plain_text(plain, d, url)
    pdf_path = DAYS / f"{d.isoformat()}.pdf"
    # do not store the binary in git by default — text JSON is the record
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
            "status": "no_sitting",
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



HTML_START = date(2003, 2, 1)


def backfill_chunk(index: dict[str, Any], n: int, delay: float, force: bool = False) -> tuple[int, int]:
    """Walk backward toward Feb 2003, checking up to n weekdays, then stop.

    Cursor is stored on the index so the next run continues where this one
    left off. Already-saved days are skipped with no web request.
    Weekends are skipped (the House almost never sits).
    """
    if index.get("backfill_done"):
        print("Backfill already reached 2003-02-01.")
        return 0, 0

    raw = index.get("backfill_cursor")
    try:
        cursor = date.fromisoformat(raw) if raw else date.today()
    except ValueError:
        cursor = date.today()

    if cursor < HTML_START:
        index["backfill_done"] = True
        index["backfill_cursor"] = HTML_START.isoformat()
        print("Backfill already reached 2003-02-01.")
        return 0, 0

    print(f"Backfill: up to {n} weekdays, starting just before {cursor.isoformat()}")
    checked = 0
    ok = 0
    missing = 0
    d = cursor
    # safety cap so a bad loop cannot run forever
    guard = 0
    while checked < n and d >= HTML_START and guard < 4000:
        guard += 1
        d = d - timedelta(days=1)
        if d < HTML_START:
            break
        if d.weekday() >= 5:
            continue
        key = d.isoformat()
        already = index.get("days", {}).get(key)
        if already and already.get("status") in ("ok", "missing") and not force:
            continue
        rec = scrape_day(d, delay=delay, force=force)
        update_index_for_day(index, rec, d)
        checked += 1
        if rec:
            ok += 1
        else:
            missing += 1
        if checked % 5 == 0:
            index["backfill_cursor"] = d.isoformat()
            save_index(index)

    index["backfill_cursor"] = d.isoformat() if d >= HTML_START else HTML_START.isoformat()
    if d <= HTML_START:
        index["backfill_done"] = True
        print("Backfill reached 2003-02-01. Historical HTML pass is complete.")
    else:
        print(f"Backfill pause at {index['backfill_cursor']}. Next run continues from there.")
    return ok, missing


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
    ap.add_argument(
        "--backfill-days",
        type=int,
        metavar="N",
        default=0,
        help="Walk N weekdays further back toward 2003-02-01 and save the cursor",
    )
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
    elif args.backfill_days:
        dates = []
    else:
        ap.print_help()
        print(
            "\nExamples:\n"
            "  python scripts/scrape_hansard.py --recent 14 --backfill-days 40\n"
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

    if args.backfill_days:
        bok, bmiss = backfill_chunk(index, n=args.backfill_days, delay=args.delay, force=args.force)
        ok += bok
        missing += bmiss

    save_index(index)
    print(f"\nDone. ok={ok} missing={missing} index_days={len(index['days'])} cursor={index.get('backfill_cursor')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
