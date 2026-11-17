"""Official NFL injury designations, week by week, from nflverse's public data.

Two files per season, both published on GitHub by the nflverse project:

  injuries/injuries_<year>.csv          the NFL's weekly game-status report
                                        (Out, Doubtful, Questionable) with the injury
  weekly_rosters/roster_weekly_<year>.csv  every team's roster each game week,
                                        with the player's league status code
                                        (active, injured reserve, PUP, suspended, ...)

Neither needs a login. Only GET requests are made. The files are slimmed to
fantasy positions and cached as cache/<year>/nfl_status.json; finished seasons
are fetched once and kept for good.

A team has no rows in its bye week, so a player who appears in a week's roster
file was on a team that played that week. Each player keeps the list of those
weeks ("seen"); a seen week without an unavailable status means he was available.
"""
from __future__ import annotations

import csv
import io
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

ROOT = Path(__file__).resolve().parent
BASE = "https://github.com/nflverse/nflverse-data/releases/download/"
POSITIONS = {"QB", "RB", "FB", "WR", "TE", "K"}

# League roster status codes that mean a player is unavailable. Codes not listed
# (practice squad, released, retired, and a few rare reserve codes) are ignored.
ROSTER_CODES = {
    "R01": "IR",    # Reserve/Injured
    "R48": "IR",    # Reserve/Injured, designated for return
    "R04": "PUP",   # Reserve/Physically unable to perform
    "R05": "NFI",   # Reserve/Non-football injury
    "E02": "SUSP",  # Exempt list (suspensions)
}
REPORT_CODES = {"Out": "OUT", "Doubtful": "DOUBTFUL"}
VERSION = 2  # bump when the cached shape changes


class NflDataError(Exception):
    pass


def _rows(path: str) -> Iterator[Dict[str, str]]:
    req = urllib.request.Request(BASE + path, method="GET",
                                 headers={"User-Agent": "fantasy-dashboard"})
    last: Optional[Exception] = None
    for attempt in range(3):
        if attempt:
            time.sleep(2 ** attempt)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read()
            return csv.DictReader(io.StringIO(body.decode("utf-8")))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NflDataError("No NFL data published at %s" % path)
            last = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
    raise NflDataError("NFL data download failed for %s: %s" % (path, last))


def _espn_id(value: str) -> Optional[int]:
    try:
        return int(float(value)) if value else None
    except ValueError:
        return None


def _download(year: int) -> Dict[str, Any]:
    players: Dict[str, Dict[str, Any]] = {}

    def player(row: Dict[str, str]) -> Dict[str, Any]:
        p = players.setdefault(row["gsis_id"], {
            "espn": None, "name": (row.get("full_name") or "").strip(),
            "pos": row.get("position"), "weeks": {}, "seen": set()})
        p["seen"].add(int(row["week"]))
        p["espn"] = p["espn"] or _espn_id(row.get("espn_id", ""))
        return p

    for row in _rows("weekly_rosters/roster_weekly_%d.csv" % year):
        if row.get("game_type") != "REG" or row.get("position") not in POSITIONS:
            continue
        if not row.get("gsis_id"):
            continue
        p = player(row)
        code = ROSTER_CODES.get(row.get("status_description_abbr") or "")
        if code:
            p["weeks"][row["week"]] = [code, ""]
    for row in _rows("injuries/injuries_%d.csv" % year):
        if row.get("game_type") != "REG" or row.get("position") not in POSITIONS:
            continue
        code = REPORT_CODES.get(row.get("report_status") or "")
        if not code or not row.get("gsis_id"):
            continue
        injury = (row.get("report_primary_injury") or "").strip()
        if injury.lower().startswith("not injury related"):
            continue  # rest days, personal matters
        p = player(row)
        if row["week"] not in p["weeks"]:  # a reserve-list status takes precedence
            p["weeks"][row["week"]] = [code, injury]
    for p in players.values():
        p["seen"] = sorted(p["seen"])
    return {"season": year, "version": VERSION, "fetched": int(time.time()),
            "players": players}


def season_status(year: int, cache_root: Optional[Path] = None, refresh: bool = False
                  ) -> Dict[str, Any]:
    """Slimmed NFL status for one season, from cache unless refresh is set.

    A failed download falls back to whatever is cached; with nothing cached it
    raises NflDataError.
    """
    path = (cache_root or ROOT / "cache") / str(year) / "nfl_status.json"
    if path.exists() and not refresh:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("version") == VERSION:
            return cached
    try:
        data = _download(year)
    except NflDataError:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        raise
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)
    return data
