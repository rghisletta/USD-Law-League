"""Build a static copy of the dashboard in site/ (for GitHub Pages).

    python3 build_static.py

Pulls the league (read-only GETs), then writes site/index.html and
site/data.json. The slow-moving data (season totals, trades) is refetched at
most every few hours; the current week is refetched every run.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import analytics
import ingest
from espn_client import EspnClient, EspnError

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
FULL_EVERY = 4 * 3600  # seconds


def main() -> int:
    try:
        client = EspnClient.from_env()
        marker = client.cache_dir / "players_weekly.json"
        fresh = marker.exists() and time.time() - marker.stat().st_mtime < FULL_EVERY
        ingest.ingest(client, light=fresh)
    except EspnError as e:
        print("Build failed: %s" % e, file=sys.stderr)
        return 1
    conn = sqlite3.connect(str(ingest.DB_PATH))
    data = analytics.dashboard(conn, int(time.time() * 1000))
    conn.close()
    SITE.mkdir(exist_ok=True)
    shutil.copyfile(str(ROOT / "static" / "index.html"), str(SITE / "index.html"))
    (SITE / "data.json").write_text(json.dumps(data), encoding="utf-8")
    (SITE / ".nojekyll").write_text("", encoding="utf-8")
    print("Wrote %s (%d KB of data)" % (SITE, (SITE / "data.json").stat().st_size // 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
