#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["beautifulsoup4"]
# ///
"""Fetch Radio Easy Rock's playlist pages from onlineradiobox and merge them
into data/days/YYYY-MM-DD.json, one file per station-day.

Raw only: no normalising, no filtering of station idents. That is build.py's
job, so rule changes can be replayed over the whole history.

Usage:
  ./scrape.py                 # fetch today + previous 6 days, merge
  ./scrape.py --pages 1 2     # just those pages (n = days before today)
  ./scrape.py --html page.html --dry-run   # parse a saved page, print result
"""
import argparse
import json
import re
import sys
import time
import urllib.request
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

BASE = "https://onlineradiobox.com/it/easyrock/playlist/"
DAYS_DIR = Path(__file__).resolve().parent / "data" / "days"
PADOVA = ZoneInfo("Europe/Rome")  # station is in Padova; IANA has no Padova zone
UA = "Mozilla/5.0 (compatible; easyrock-catalog; personal archive, ~1 run/day)"

TIME_RE = re.compile(r"\d{2}:\d{2}")
# Day tab, e.g. "Dom 22.11" / "Merc 18.11". The active (displayed) day is the
# only tab that isn't a link.
TAB_RE = re.compile(r"(?:Lun|Mar|Mer|Merc|Gio|Ven|Sab|Dom)\.?\s+(\d{1,2})\.(\d{1,2})")
TRACK_RE = re.compile(r"/track/(\d+)")


def fetch(n: int) -> str:
    url = BASE if n == 0 else f"{BASE}{n}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "it"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def page_date(soup: BeautifulSoup, today: date) -> date:
    """Date of the displayed day, read from the unlinked day tab.

    Not derived from now() - n: Actions runs on UTC, the station on Padova (Italian) time,
    and the site's idea of "today" is what counts.
    """
    found = set()
    for tag in soup.find_all(True):
        text = " ".join(tag.get_text(" ", strip=True).split())
        m = TAB_RE.fullmatch(text)
        if m and tag.name != "a" and tag.find_parent("a") is None and tag.find("a") is None:
            found.add((int(m[1]), int(m[2])))
    if len(found) != 1:
        raise ValueError(f"expected one active day tab, found {sorted(found)}")
    (day, month), = found
    year = today.year - 1 if month > today.month else today.year  # Dec→Jan
    d = date(year, month, day)
    if not 0 <= (today - d).days <= 8:
        raise ValueError(f"page date {d} is implausibly far from {today}")
    return d


def parse(html: str, today: date) -> tuple[date, list[dict]]:
    soup = BeautifulSoup(html, "html.parser")
    d = page_date(soup, today)
    plays = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        t = tds[0].get_text(strip=True)
        if not TIME_RE.fullmatch(t):  # skips the untimed "In diretta" row
            continue
        raw = " ".join(tds[1].get_text(" ", strip=True).split())
        a = tds[1].find("a", href=TRACK_RE)
        plays.append({
            "time": t,
            "raw": raw,
            "track_id": TRACK_RE.search(a["href"])[1] if a else None,
        })
    if not plays:
        raise ValueError(f"no timed rows found for {d}; markup changed?")
    return d, plays


def merge(old: list[dict], new: list[dict]) -> list[dict]:
    """Union keyed on (time, raw). Never deletes.

    A key seen k times in one scrape (same row twice in a minute) is kept
    max(k_old, k_new) times, so re-scraping never duplicates.
    """
    key = lambda p: (p["time"], p["raw"])
    co, cn = Counter(map(key, old)), Counter(map(key, new))
    ids = {}
    for p in old + new:
        ids[key(p)] = ids.get(key(p)) or p["track_id"]
    out = []
    for k in sorted(co.keys() | cn.keys()):
        out += [{"time": k[0], "raw": k[1], "track_id": ids[k]}] * max(co[k], cn[k])
    return out


def dump(d: date, plays: list[dict]) -> str:
    # One play per line: readable git diffs.
    rows = ",\n".join("    " + json.dumps(p, ensure_ascii=False) for p in plays)
    return f'{{\n  "date": "{d}",\n  "plays": [\n{rows}\n  ]\n}}\n'


def save(d: date, plays: list[dict], dry_run: bool) -> str:
    path = DAYS_DIR / f"{d}.json"
    old = json.loads(path.read_text())["plays"] if path.exists() else []
    merged = merge(old, plays)
    text = dump(d, merged)
    status = f"{d}: {len(merged)} rows (+{len(merged) - len(old)})"
    if dry_run:
        print(text)
    elif not path.exists() or path.read_text() != text:
        DAYS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return status


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pages", type=int, nargs="*", default=list(range(7)))
    ap.add_argument("--html", type=Path, help="parse a saved page instead of fetching")
    ap.add_argument("--dry-run", action="store_true", help="print, don't write")
    args = ap.parse_args()

    today = datetime.now(PADOVA).date()
    sources = [(args.html.name, lambda: args.html.read_text())] if args.html else [
        (f"page {n}", lambda n=n: fetch(n)) for n in args.pages
    ]
    failed = 0
    for i, (label, get) in enumerate(sources):
        if i:
            time.sleep(2)  # be gentle
        try:
            d, plays = parse(get(), today)
            print(f"{label} → {save(d, plays, args.dry_run)}", file=sys.stderr)
        except Exception as e:  # keep going; one bad page shouldn't sink the rest
            failed += 1
            print(f"{label}: FAILED {type(e).__name__}: {e}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
