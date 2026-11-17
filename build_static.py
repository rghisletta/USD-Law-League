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


def _diagnose() -> None:
    """On failure, print what can be checked without revealing the cookies."""
    import os
    import urllib.error
    import urllib.request
    from espn_client import clean_s2, clean_swid
    s2 = clean_s2(os.environ.get("ESPN_S2", ""))
    swid = clean_swid(os.environ.get("ESPN_SWID", ""))
    print("diagnose: ESPN_S2 length %d, has %%-escapes: %s, has whitespace: %s" % (
        len(s2), "%" in s2, any(c.isspace() for c in s2)))
    print("diagnose: ESPN_SWID length %d, braces: %s" % (
        len(swid), swid.startswith("{") and swid.endswith("}")))
    league = os.environ.get("LEAGUE_ID", "")
    for season in ("2026", "2025"):
        for label, cookie in (("with cookies", True), ("no cookies", False)):
            url = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/%s"
                   "/segments/0/leagues/%s?view=mStatus" % (season, league))
            headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
            if cookie and s2 and swid:
                sw = swid if swid.startswith("{") else "{" + swid.strip("{}") + "}"
                headers["Cookie"] = "espn_s2=%s; SWID=%s" % (s2, sw)
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                            timeout=30) as r:
                    print("diagnose: season %s %s -> HTTP %d" % (season, label, r.status))
            except urllib.error.HTTPError as e:
                print("diagnose: season %s %s -> HTTP %d %s" % (
                    season, label, e.code, e.read().decode("utf-8", "replace")[:200]))
            except Exception as e:  # noqa: BLE001
                print("diagnose: season %s %s -> %s" % (season, label, e))


def _cookie_shape() -> None:
    """Print the cookies' shape (never their values) to help spot paste mistakes."""
    import os
    from espn_client import clean_s2, clean_swid
    s2 = clean_s2(os.environ.get("ESPN_S2", ""))
    swid = clean_swid(os.environ.get("ESPN_SWID", ""))
    if s2 or swid:
        print("cookies: ESPN_S2 %d chars (spaces/newlines: %s, starts with 'espn_s2': %s); "
              "ESPN_SWID %d chars (braces: %s, spaces/newlines: %s, dashes: %d)" % (
                  len(s2), any(c.isspace() for c in s2), s2.lower().startswith("espn_s2"),
                  len(swid), swid.strip().startswith("{") and swid.strip().endswith("}"),
                  any(c.isspace() for c in swid), swid.count("-")))


def main() -> int:
    _cookie_shape()
    try:
        client = EspnClient.from_env()
        marker = client.cache_dir / "players_weekly.json"
        fresh = marker.exists() and time.time() - marker.stat().st_mtime < FULL_EVERY
        ingest.ingest(client, light=fresh)
    except EspnError as e:
        print("Build failed: %s" % e, file=sys.stderr)
        _diagnose()
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
