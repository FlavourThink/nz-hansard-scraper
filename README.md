# NZ Hansard Daily Scraper

Scrapes New Zealand Parliament Hansard (debate transcripts) into JSON, designed to live in a GitHub repo and update daily via GitHub Actions.

## Important coverage limits

| Period | Availability | This tool |
|--------|--------------|-----------|
| **Feb 2003 → present** | Official HTML on parliament.nz | ✅ Supported |
| **1987 – 2002** | PDF volumes | ❌ Not in this scraper (needs PDF/OCR pipeline) |
| **1854 – 1986** | OCR of printed volumes | ❌ Not in this scraper |

Winston Peters first entered Parliament on **25 November 1978**.  
Fully structured HTML Hansard only goes back to **February 2003** (Volume 606, 47th Parliament).  
This tool therefore targets **2003 → today**. Pre-2003 requires a separate PDF/OCR project.

## What you get

- `data/hansard_index.json` — master index of all scraped sitting days
- `data/days/YYYY-MM-DD.json` — one file per sitting day (speeches, questions, metadata)
- Daily GitHub Action that:
  1. Scrapes any new sitting days
  2. Commits updated JSON back to the repo

## Existing tools (reference)

These were useful starting points; none are production-ready daily updaters:

- [mjdall/parliament-scraper](https://github.com/mjdall/parliament-scraper) — Python, old `HansD_` URL pattern, incomplete
- [nathanielw/hansard-scraper](https://github.com/nathanielw/hansard-scraper) — Node, oral questions only
- [TeHikuMedia/nga-tautohetohe](https://github.com/TeHikuMedia/nga-tautohetohe) — historical + te reo extraction
- [edithatogo/nz-hansard-corpus](https://huggingface.co/datasets/edithatogo/nz-hansard-corpus) — structured corpus (Parliaments 47–54)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Usage

### Scrape a single day
```bash
python scripts/scrape_hansard.py --date 2024-08-14
```

### Backfill from 2003 (or any start date)
```bash
# First run — builds historical set (can take hours; be polite)
python scripts/scrape_hansard.py --from 2003-02-01 --to 2026-08-16 --delay 2.5
```

### Daily incremental (what the Action runs)
```bash
python scripts/scrape_hansard.py --recent 14 --delay 2
```
Looks back 14 calendar days and only fetches days not already in the index.

### Rebuild index only
```bash
python scripts/scrape_hansard.py --reindex
```

## GitHub Actions (daily)

See `.github/workflows/daily-scrape.yml`.

1. Push this repo to GitHub
2. Enable Actions
3. The workflow runs every day at 18:00 UTC (~06:00 NZST next morning)
4. It commits new/updated JSON into `data/`

Optional: add a `HANSARD_USER_AGENT` secret if you want a custom contact string in the User-Agent.

## JSON shape (per day)

```json
{
  "date": "2024-08-14",
  "url": "https://www.parliament.nz/...",
  "scraped_at": "2026-08-16T06:00:00+00:00",
  "title": "Wednesday, 14 August 2024",
  "sections": [
    {
      "heading": "Oral Questions",
      "items": [
        {
          "type": "question",
          "speaker": "…",
          "text": "…"
        }
      ]
    }
  ],
  "raw_text_length": 123456
}
```

## Be a good citizen

- Default delay between requests is 2 seconds
- Do not hammer the site with parallel workers
- Parliament pages are public; still respect their infrastructure
- This is unofficial — not endorsed by the Office of the Clerk

## Licence

MIT. Hansard text itself remains subject to parliamentary copyright / terms of use on parliament.nz.
