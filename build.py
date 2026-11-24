#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["sqlite-utils", "titlecase"]
# ///
"""Rebuild playlist.db from scratch out of data/days/*.json.

All normalising lives here, so changing a rule (or overrides.csv) and
re-running applies it to the whole history.

  ./build.py                 # → playlist.db
  uvx datasette playlist.db -m metadata.yml
"""
import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import sqlite_utils
from titlecase import titlecase

ROOT = Path(__file__).resolve().parent
DAYS_DIR = ROOT / "data" / "days"
DB_PATH = ROOT / "playlist.db"
OVERRIDES = ROOT / "overrides.csv"

PADOVA = ZoneInfo("Europe/Rome")  # station is in Padova; IANA has no Padova zone
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

IDENT_PREFIX = "WWW.RADIOEASYROCK.IT - "
AD = "RADIO EASY ROCK - PUBBLICITA"
VERSION_RE = re.compile(r"\s+-\s+(RNB)$")  # trailing version tags, not part of the title
FEAT_RE = re.compile(r"\s+(?:FEAT|FT|FEATURING)\.?\s+")
DOTTED_RE = re.compile(r"(?<![\w.])[A-Za-z](?:\.[A-Za-z])+\.?(?!\w)")  # D.C., R.E.M., T.N.T


def squash(s: str) -> str:
    return " ".join(s.split()).upper()


def load_overrides() -> dict[str, str]:
    """overrides.csv: from,to (case-insensitive `from`, verbatim `to`).

    - Matching a whole artist name or title replaces it outright.
    - A single-word `from` (no spaces) also replaces that word wherever it
      appears after title-casing: MJ fixes "Mj Lenderman" in every credit."""
    if not OVERRIDES.exists():
        return {}
    with OVERRIDES.open(newline="") as f:
        return {squash(r["from"]): r["to"].strip() for r in csv.DictReader(f) if r["from"].strip()}


def display(s: str, overrides: dict[str, str]) -> str:
    if s in overrides:
        return overrides[s]
    t = DOTTED_RE.sub(lambda m: m[0].upper(), titlecase(s.lower()))
    for word, to in overrides.items():
        if " " not in word:
            t = re.sub(rf"(?<![\w/']){re.escape(word)}(?![\w/'])", to, t, flags=re.I)
    return t


def classify(raw: str) -> str:
    u = squash(raw)
    if u.startswith(IDENT_PREFIX):
        return "ident"
    if u == AD:
        return "ad"
    return "song"


def split_song(raw: str) -> tuple[list[str], str, str | None]:
    """'A FEAT B - TITLE - RNB' → (['A', 'B'], 'TITLE', 'RNB').
    Splits on the FIRST ' - ': titles can contain ' - ', artists don't."""
    u = squash(raw)
    version = None
    if m := VERSION_RE.search(u):
        version, u = m[1], u[: m.start()]
    artist, _, title = u.partition(" - ")
    return FEAT_RE.split(artist), title, version


def load_rows():
    for f in sorted(DAYS_DIR.glob("*.json")):
        day = json.loads(f.read_text())
        for p in day["plays"]:
            yield day["date"], p["time"], p["raw"]


def build():
    overrides = load_overrides()
    songs: dict[tuple[str, str], dict] = {}  # (credit, title) display → row
    plays = []

    for i, (date, hhmm, raw) in enumerate(sorted(load_rows())):
        local = datetime.fromisoformat(f"{date}T{hhmm}").replace(tzinfo=PADOVA)
        utc = local.astimezone(timezone.utc)
        kind = classify(raw)
        song_id = version = None
        if kind == "song":
            credits, title, version = split_song(raw)
            parts = [display(c, overrides) for c in credits]
            key = (" feat. ".join(parts), display(title, overrides))
            if key not in songs:  # ids in first-played order: stable as data grows
                songs[key] = {"id": len(songs) + 1, "artist": key[0],
                              "lead_artist": parts[0], "title": key[1]}
            song_id = songs[key]["id"]
        plays.append({
            "id": i + 1,
            "played_at": local.strftime("%Y-%m-%d %H:%M"),
            "played_at_utc": utc.strftime("%Y-%m-%dT%H:%MZ"),
            "date": date,
            "hour": local.hour,
            "weekday": local.isoweekday(),
            "weekday_name": WEEKDAYS[local.weekday()],
            "kind": kind,
            "song_id": song_id,
            "version": version,
            "raw": raw,
        })

    tmp = DB_PATH.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite_utils.Database(tmp)
    db["songs"].insert_all(songs.values(), pk="id")
    db["songs"].create_index(["artist"])
    db["songs"].enable_fts(["artist", "title"], create_triggers=False)
    db["plays"].insert_all(plays, pk="id", foreign_keys=[("song_id", "songs", "id")])
    for cols in (["song_id"], ["date"], ["kind"], ["played_at"]):
        db["plays"].create_index(cols)
    db["overrides"].create({"from": str, "to": str}, pk="from")
    db["overrides"].insert_all({"from": k, "to": v} for k, v in overrides.items())
    for name, sql in VIEWS.items():
        db.create_view(name, sql)
    db.vacuum()
    db.close()
    tmp.replace(DB_PATH)  # atomic: never leave a half-built db behind

    kinds = Counter(p["kind"] for p in plays)
    print(f"{DB_PATH.name}: {len(plays)} plays ({dict(kinds)}), {len(songs)} songs, "
          f"{len({s['artist'] for s in songs.values()})} artists, "
          f"{plays[0]['date'] if plays else '-'} → {plays[-1]['date'] if plays else '-'}")


# "This week" = the last 7 days of *data*, not of the browser's clock, so the
# views stay meaningful if the scraper ever stops.
WEEK = "date > date((select max(date) from plays), '-7 days')"
SONG_PLAYS = "plays p join songs s on s.id = p.song_id"

VIEWS = {
    "song_stats": f"""
select s.id as song_id, s.artist, s.title, count(*) as plays,
  min(p.played_at) as first_played, max(p.played_at) as last_played,
  count(distinct p.date) as days_on_air,
  round((julianday(max(p.played_at_utc)) - julianday(min(p.played_at_utc))) * 24
        / nullif(count(*) - 1, 0), 1) as avg_gap_hours
from {SONG_PLAYS} group by s.id order by plays desc""",

    "artist_stats": f"""
select s.artist, count(*) as plays, count(distinct s.id) as songs,
  min(p.played_at) as first_played, max(p.played_at) as last_played
from {SONG_PLAYS} group by s.artist order by plays desc""",

    "lead_artist_stats": f"""
select s.lead_artist, count(*) as plays, count(distinct s.id) as songs,
  count(distinct s.artist) as credits,
  min(p.played_at) as first_played, max(p.played_at) as last_played
from {SONG_PLAYS} group by s.lead_artist order by plays desc""",

    "top_songs_this_week": f"""
select s.id as song_id, s.artist, s.title, count(*) as plays
from {SONG_PLAYS} where p.{WEEK} group by s.id order by plays desc, s.artist""",

    "top_artists_this_week": f"""
select s.artist, count(*) as plays, count(distinct s.id) as songs
from {SONG_PLAYS} where p.{WEEK} group by s.artist order by plays desc, s.artist""",

    "new_this_week": f"""
select * from song_stats
where first_played > (select date(max(date), '-7 days') from plays)
  and first_played >= (select date(min(date), '+7 days') from plays)
order by first_played desc""",

    "one_offs": """
select * from song_stats where plays = 1 order by first_played desc""",

    "heavy_rotation": """
select * from song_stats where plays >= 5 order by avg_gap_hours""",

    "hour_by_weekday": """
select hour,
  sum(weekday = 1) as Mon, sum(weekday = 2) as Tue, sum(weekday = 3) as Wed,
  sum(weekday = 4) as Thu, sum(weekday = 5) as Fri, sum(weekday = 6) as Sat,
  sum(weekday = 7) as Sun, count(*) as total
from plays where kind = 'song' group by hour order by hour""",

    "ads_per_hour": """
select hour, count(*) as ad_breaks, count(distinct date) as days,
  round(count(*) * 1.0 / count(distinct date), 1) as per_day
from plays where kind = 'ad' group by hour order by hour""",
}

if __name__ == "__main__":
    build()
