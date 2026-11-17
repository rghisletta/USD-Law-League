"""Pull the league from ESPN and normalise it into SQLite (league.db).

    python3 ingest.py            # completed weeks from cache, the rest refetched
    python3 ingest.py --refresh  # refetch everything
    python3 ingest.py --light    # only refetch league status and the current week

The database is rebuilt from the JSON cache on every run (into a temp file that
is swapped in at the end), so it is always consistent with the cache.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import nfl_client
from espn_client import EspnClient, EspnError

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "league.db"

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE teams (
    team_id INTEGER PRIMARY KEY, name TEXT, abbrev TEXT,
    wins INTEGER, losses INTEGER, ties INTEGER,
    points_for REAL, points_against REAL, owner_id TEXT, manager TEXT
);
CREATE TABLE players (
    player_id INTEGER PRIMARY KEY, name TEXT, position_id INTEGER,
    pro_team_id INTEGER, on_team_id INTEGER, status TEXT,
    season_points REAL, season_avg REAL, adp REAL, pct_owned REAL
);
CREATE TABLE matchups (
    week INTEGER, matchup_id INTEGER,
    home_team_id INTEGER, home_points REAL,
    away_team_id INTEGER, away_points REAL,
    winner TEXT,
    home_live REAL, away_live REAL, home_proj REAL, away_proj REAL,
    PRIMARY KEY (week, matchup_id)
);
CREATE TABLE lineups (
    team_id INTEGER, week INTEGER, player_id INTEGER,
    slot_id INTEGER, points REAL, projected REAL, pro_team_id INTEGER,
    PRIMARY KEY (team_id, week, player_id)
);
CREATE TABLE draft_picks (
    overall_pick INTEGER PRIMARY KEY, round INTEGER, round_pick INTEGER,
    team_id INTEGER, player_id INTEGER, keeper INTEGER,
    bid_amount INTEGER, auto_draft_type_id INTEGER
);
CREATE TABLE transactions (
    transaction_id TEXT PRIMARY KEY, week INTEGER, type TEXT, status TEXT,
    team_id INTEGER, proposed_date INTEGER, items TEXT
);
CREATE TABLE trades (trade_id TEXT PRIMARY KEY, date INTEGER);
CREATE TABLE trade_items (
    trade_id TEXT, player_id INTEGER, from_team_id INTEGER, to_team_id INTEGER
);
CREATE TABLE pro_teams (pro_team_id INTEGER PRIMARY KEY, abbrev TEXT, bye_week INTEGER);
CREATE TABLE pro_games (pro_team_id INTEGER, week INTEGER, date INTEGER);
CREATE TABLE managers (owner_id TEXT PRIMARY KEY, first_name TEXT, last_name TEXT);
CREATE TABLE history_teams (
    season INTEGER, team_id INTEGER, owner_id TEXT, name TEXT,
    wins INTEGER, losses INTEGER, ties INTEGER, points_for REAL, points_against REAL,
    final_rank INTEGER, playoff_seed INTEGER, made_playoffs INTEGER,
    PRIMARY KEY (season, team_id)
);
CREATE TABLE history_games (
    season INTEGER, week INTEGER, tier TEXT,
    home_team_id INTEGER, home_points REAL, away_team_id INTEGER, away_points REAL
);
CREATE TABLE player_weeks (
    season INTEGER, player_id INTEGER, week INTEGER, points REAL,
    PRIMARY KEY (season, player_id, week)
);
CREATE TABLE history_players (
    season INTEGER, player_id INTEGER, name TEXT, position_id INTEGER, points REAL,
    PRIMARY KEY (season, player_id)
);
CREATE TABLE history_picks (
    season INTEGER, overall_pick INTEGER, round INTEGER, team_id INTEGER, player_id INTEGER
);
CREATE TABLE history_rosters (
    season INTEGER, week INTEGER, team_id INTEGER, player_id INTEGER, slot_id INTEGER,
    PRIMARY KEY (season, week, player_id)
);
CREATE TABLE nfl_status (
    season INTEGER, week INTEGER, player_id INTEGER, status TEXT, injury TEXT,
    PRIMARY KEY (season, week, player_id)
);
"""


def load_names(path: Path = ROOT / "names.json") -> Dict[str, str]:
    """Real names by team id, from names.json: {"2": {"team": "...", "name": "Bryson"}}.

    The "team" entry is only there to help whoever fills the file in.
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    out = {}
    for team_id, entry in raw.items():
        name = entry.get("name") if isinstance(entry, dict) else entry
        if isinstance(name, str) and name.strip():
            out[str(team_id)] = name.strip()
    return out


def _load_side(conn: sqlite3.Connection, week: int, side: Optional[Dict[str, Any]]) -> None:
    if not side:
        return
    roster = side.get("rosterForCurrentScoringPeriod") or {}
    for entry in roster.get("entries", []):
        ppe = entry.get("playerPoolEntry") or {}
        player = ppe.get("player") or {}
        pid = entry.get("playerId")
        projected = None
        for s in player.get("stats") or []:
            if (s.get("statSourceId") == 1 and s.get("statSplitTypeId") == 1
                    and s.get("scoringPeriodId") == week):
                projected = s.get("appliedTotal")
        conn.execute(
            "INSERT OR IGNORE INTO players (player_id, name, position_id, pro_team_id) "
            "VALUES (?,?,?,?)",
            (pid, (player.get("fullName") or "").strip(),
             player.get("defaultPositionId"), player.get("proTeamId")))
        conn.execute("INSERT OR REPLACE INTO lineups VALUES (?,?,?,?,?,?,?)",
                     (side["teamId"], week, pid, entry.get("lineupSlotId"),
                      ppe.get("appliedStatTotal") or 0.0, projected,
                      player.get("proTeamId")))


def _upsert_player(conn: sqlite3.Connection, p: Dict[str, Any]) -> None:
    if not p.get("name"):
        return
    conn.execute("INSERT OR REPLACE INTO players VALUES (?,?,?,?,?,?,?,?,?,?)", (
        p["id"], p["name"], p.get("positionId"), p.get("proTeamId"),
        p.get("onTeamId"), p.get("status"), p.get("seasonPoints"),
        p.get("seasonAvg"), p.get("adp"), p.get("pctOwned")))


def _load_weeks(conn: sqlite3.Connection, season: int, p: Dict[str, Any]) -> None:
    for week, pts in (p.get("weekly") or {}).items():
        conn.execute("INSERT OR REPLACE INTO player_weeks VALUES (?,?,?,?)",
                     (season, p["id"], int(week), pts))


_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_NFL_POS = {1: {"QB"}, 2: {"RB", "FB"}, 3: {"WR"}, 4: {"TE"}, 5: {"K"}}


def _name_key(name: str) -> str:
    words = "".join(c for c in name.lower() if c.isalnum() or c == " ").split()
    return " ".join(w for w in words if w not in _SUFFIXES)


def _load_nfl_status(conn: sqlite3.Connection, seasons: Set[int], current: int,
                     refresh: bool) -> Dict[str, int]:
    """Official weekly injury status (see nfl_client), keyed by ESPN player id.

    nflverse carries ESPN ids for most players. The rest are matched on name and
    position against the players this league has drafted or rostered.
    """
    data = {}
    for year in sorted(seasons):
        try:  # finished seasons come from cache; the current one is refetched
            data[year] = nfl_client.season_status(year, refresh=refresh and year == current)
        except nfl_client.NflDataError:
            continue  # a nice-to-have; never block the rest of the ingest
    to_espn: Dict[str, int] = {}
    info: Dict[str, Tuple[str, str]] = {}
    for d in data.values():
        for gsis, p in d["players"].items():
            if p.get("espn"):
                to_espn[gsis] = p["espn"]
            info[gsis] = (p["name"], p["pos"])
    by_name: Dict[str, List[str]] = {}
    for gsis, (name, _) in info.items():
        by_name.setdefault(_name_key(name), []).append(gsis)
    ours = {pid: (name, pos) for pid, name, pos in conn.execute(
        """SELECT player_id, name, position_id FROM players WHERE player_id IN
           (SELECT player_id FROM draft_picks UNION SELECT player_id FROM history_picks
            UNION SELECT player_id FROM history_rosters
            UNION SELECT player_id FROM lineups)""") if name}
    known = set(to_espn.values())
    matched = unmatched = 0
    for pid, (name, pos) in ours.items():
        if pid in known or pos not in _NFL_POS:
            continue
        hits = [g for g in by_name.get(_name_key(name), [])
                if g not in to_espn and info[g][1] in _NFL_POS[pos]]
        if len(hits) == 1:
            to_espn[hits[0]] = pid
            matched += 1
        else:
            unmatched += 1
    for year, d in data.items():
        for gsis, p in d["players"].items():
            pid = to_espn.get(gsis)
            if not pid:
                continue
            for week in p.get("seen") or []:
                code, injury = p["weeks"].get(str(week)) or ("ACT", "")
                conn.execute("INSERT OR REPLACE INTO nfl_status VALUES (?,?,?,?,?)",
                             (year, week, pid, code, injury or None))
    return {"name_matched": matched, "unmatched": unmatched}


def ingest(client: Any, db_path: Path = DB_PATH, refresh: bool = False,
           light: bool = False) -> Dict[str, int]:
    """refresh: refetch everything. light: refetch only league + current week."""
    league = client.league(refresh=True)
    status = league["status"]
    settings = league["settings"]
    current = int(status["currentMatchupPeriod"])
    reg_weeks = int(settings["scheduleSettings"]["matchupPeriodCount"])
    last_week = min(current, int(status.get("finalScoringPeriod") or current))
    slow = not light  # refetch the slower-moving data too

    tmp_path = Path(str(db_path) + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    conn = sqlite3.connect(str(tmp_path))
    conn.executescript(SCHEMA)

    meta = {
        "league_name": (settings.get("name") or "").strip(),
        "season": league.get("seasonId"),
        "current_week": current,
        "last_completed_week": current - 1,
        "regular_season_weeks": reg_weeks,
        "playoff_team_count": settings["scheduleSettings"].get("playoffTeamCount"),
        "lineup_slot_counts": json.dumps(
            settings.get("rosterSettings", {}).get("lineupSlotCounts", {})),
        "my_team_id": getattr(client, "my_team_id", None),
        "ingested_at": int(time.time() * 1000),
    }
    conn.executemany("INSERT INTO meta VALUES (?,?)",
                     [(k, None if v is None else str(v)) for k, v in meta.items()])

    real_names = load_names()
    for t in league["teams"]:
        rec = (t.get("record") or {}).get("overall") or {}
        conn.execute("INSERT INTO teams VALUES (?,?,?,?,?,?,?,?,?,?)", (
            t["id"], (t.get("name") or "").strip(), (t.get("abbrev") or "").strip(),
            rec.get("wins"), rec.get("losses"), rec.get("ties"),
            rec.get("pointsFor"), rec.get("pointsAgainst"), t.get("primaryOwner"),
            real_names.get(str(t["id"]))))
    for m in league.get("members", []):
        conn.execute("INSERT OR REPLACE INTO managers VALUES (?,?,?)",
                     (m.get("id"), m.get("firstName"), m.get("lastName")))

    wanted: Set[int] = set()  # player ids we need names / season totals for
    for week in range(1, last_week + 1):
        is_current = week >= current  # completed weeks never change
        data = client.matchups(week, refresh=refresh or is_current)
        for m in data.get("schedule", []):
            if m.get("matchupPeriodId") != week:
                continue
            home, away = m.get("home") or {}, m.get("away") or {}
            conn.execute("INSERT OR REPLACE INTO matchups VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                week, m.get("id"),
                home.get("teamId"), home.get("totalPoints"),
                away.get("teamId"), away.get("totalPoints"),
                m.get("winner"),
                home.get("totalPointsLive"), away.get("totalPointsLive"),
                home.get("totalProjectedPointsLive"), away.get("totalProjectedPointsLive")))
            _load_side(conn, week, home)
            _load_side(conn, week, away)

        tx = client.transactions(week, refresh=refresh or (is_current and slow))
        for t in tx.get("transactions", []) or []:
            items = t.get("items") or []
            wanted.update(i["playerId"] for i in items if i.get("playerId"))
            conn.execute("INSERT OR REPLACE INTO transactions VALUES (?,?,?,?,?,?,?)", (
                str(t.get("id")), week, t.get("type"), t.get("status"),
                t.get("teamId"), t.get("proposedDate"), json.dumps(items)))

    draft = client.draft(refresh=refresh)
    for p in (draft.get("draftDetail") or {}).get("picks", []):
        wanted.add(p.get("playerId"))
        conn.execute("INSERT OR REPLACE INTO draft_picks VALUES (?,?,?,?,?,?,?,?)", (
            p.get("overallPickNumber"), p.get("roundId"), p.get("roundPickNumber"),
            p.get("teamId"), p.get("playerId"), int(bool(p.get("keeper"))),
            p.get("bidAmount"), p.get("autoDraftTypeId")))

    for tr in client.trades(refresh=refresh or slow):
        conn.execute("INSERT OR REPLACE INTO trades VALUES (?,?)", (tr["id"], tr["date"]))
        for it in tr["items"]:
            wanted.add(it["playerId"])
            conn.execute("INSERT INTO trade_items VALUES (?,?,?,?)",
                         (tr["id"], it["playerId"], it["from"], it["to"]))

    for t in client.pro_schedule(refresh=refresh):
        conn.execute("INSERT OR REPLACE INTO pro_teams VALUES (?,?,?)",
                     (t["id"], t.get("abbrev"), t.get("byeWeek")))
        for wk, dates in (t.get("games") or {}).items():
            for d in dates:
                conn.execute("INSERT INTO pro_games VALUES (?,?,?)", (t["id"], int(wk), d))

    # Earlier seasons are finished, so they are fetched once and cached for good.
    for year in status.get("previousSeasons") or []:
        try:
            h = client.history(int(year))
        except EspnError:
            continue  # history is a nice-to-have; never block the current season
        for m in h["members"]:
            conn.execute("INSERT OR IGNORE INTO managers VALUES (?,?,?)",
                         (m["id"], m.get("firstName"), m.get("lastName")))
        cut = h.get("playoffTeamCount") or 0
        for t in h["teams"]:
            r = t["record"]
            seed = t.get("playoffSeed")
            conn.execute("INSERT OR REPLACE INTO history_teams VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                h["season"], t["id"], t.get("owner"), t["name"], r.get("wins"),
                r.get("losses"), r.get("ties"), r.get("pointsFor"), r.get("pointsAgainst"),
                t.get("finalRank"), seed, int(bool(seed and seed <= cut))))
        for g in h["games"]:
            conn.execute("INSERT INTO history_games VALUES (?,?,?,?,?,?,?)", (
                h["season"], g["week"], g["tier"], g["home"], g["homePoints"],
                g["away"], g["awayPoints"]))
        for p in h["picks"]:
            wanted.add(p["playerId"])
            conn.execute("INSERT INTO history_picks VALUES (?,?,?,?,?)", (
                h["season"], p["overall"], p["round"], p["teamId"], p["playerId"]))
        try:  # who had whom each week, through the fantasy final (week 17)
            for week in range(1, 18):
                for tid, pid, slot in client.week_rosters(int(year), week):
                    conn.execute("INSERT OR REPLACE INTO history_rosters VALUES (?,?,?,?,?)",
                                 (h["season"], week, tid, pid, slot))
                    wanted.add(pid)
        except EspnError:
            pass
        try:  # that season's final point totals, for grading its draft
            for p in client.players_season(int(year)):
                if p.get("seasonPoints") is not None:
                    conn.execute("INSERT OR REPLACE INTO history_players VALUES (?,?,?,?,?)", (
                        h["season"], p["id"], p["name"], p.get("positionId"),
                        p["seasonPoints"]))
                _load_weeks(conn, h["season"], p)
        except EspnError:
            pass

    # Season totals: top scorers league-wide, plus anyone else we reference.
    top = client.players_top(refresh=refresh or slow)
    for p in top:
        _upsert_player(conn, p)
        _load_weeks(conn, int(league.get("seasonId") or 0), p)
    wanted.update(r[0] for r in conn.execute("SELECT player_id FROM players"))
    wanted -= {p["id"] for p in top}
    wanted.discard(None)
    for p in client.players_by_ids(wanted, refresh=refresh or slow):
        _upsert_player(conn, p)

    seasons = {int(y) for y in status.get("previousSeasons") or []}
    seasons.add(int(league.get("seasonId") or 0))
    nfl = _load_nfl_status(conn, seasons, int(league.get("seasonId") or 0), refresh or slow)
    conn.execute("INSERT INTO meta VALUES (?,?)", ("nfl_name_matched", str(nfl["name_matched"])))

    conn.commit()
    counts = {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
              for t in ("teams", "players", "matchups", "lineups", "draft_picks",
                        "transactions", "trades", "pro_games", "nfl_status")}
    conn.close()
    os.replace(str(tmp_path), str(db_path))
    return counts


def main() -> int:
    try:
        counts = ingest(EspnClient.from_env(), refresh="--refresh" in sys.argv,
                        light="--light" in sys.argv)
    except EspnError as e:
        print("Ingest failed: %s" % e, file=sys.stderr)
        return 1
    for table, n in counts.items():
        print("%-13s %d rows" % (table, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
