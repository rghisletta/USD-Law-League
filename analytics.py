"""Pure functions over league.db. No network access."""
from __future__ import annotations

import json
import random
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

BENCH, IR, FLEX = 20, 21, 23

# lineup slot id -> player position ids allowed in it
SLOT_POSITIONS: Dict[int, Tuple[int, ...]] = {
    0: (1,),        # QB
    2: (2,),        # RB
    4: (3,),        # WR
    6: (4,),        # TE
    16: (16,),      # D/ST
    17: (5,),       # K
    FLEX: (2, 3, 4),
}
DEFAULT_SLOT_COUNTS = {0: 1, 2: 2, 4: 2, 6: 1, FLEX: 1, 16: 1, 17: 1}


def _meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def last_completed_week(conn: sqlite3.Connection) -> int:
    return int(_meta(conn, "last_completed_week") or 0)


def starting_slots(conn: sqlite3.Connection) -> Dict[int, int]:
    """Starting slot counts from league settings (bench/IR/unused slots dropped)."""
    raw = _meta(conn, "lineup_slot_counts")
    counts = {int(k): int(v) for k, v in json.loads(raw).items()} if raw else {}
    slots = {s: n for s, n in counts.items() if n > 0 and s in SLOT_POSITIONS}
    return slots or dict(DEFAULT_SLOT_COUNTS)


def optimal_points(players: List[Tuple[int, float]], slots: Dict[int, int]) -> float:
    """Best legal lineup total. players = [(position_id, points)].

    Dedicated slots are filled with the top scorers at each position, then FLEX
    takes the best remaining RB/WR/TE. Greedy is exact for this slot structure.
    """
    remaining = sorted(players, key=lambda p: p[1], reverse=True)
    total = 0.0
    for slot in sorted(slots, key=lambda s: s == FLEX):  # FLEX last
        for _ in range(slots[slot]):
            for i, (pos, pts) in enumerate(remaining):
                if pos in SLOT_POSITIONS[slot]:
                    total += pts
                    del remaining[i]
                    break
    return total


def bench_weeks(conn: sqlite3.Connection, through_week: Optional[int] = None,
                include_ir: bool = False) -> List[Dict[str, Any]]:
    """One row per team per week: actual starters vs best lineup in hindsight."""
    through_week = through_week or last_completed_week(conn)
    slots = starting_slots(conn)
    rows = conn.execute(
        """SELECT l.team_id, l.week, l.slot_id, l.points, p.position_id, p.name
           FROM lineups l JOIN players p USING (player_id)
           WHERE l.week <= ? ORDER BY l.team_id, l.week""", (through_week,)).fetchall()
    grouped: Dict[Tuple[int, int], List[Tuple]] = {}
    for r in rows:
        grouped.setdefault((r[0], r[1]), []).append(r[2:])

    out = []
    for (team_id, week), entries in sorted(grouped.items()):
        actual = sum(pts for slot, pts, _, _ in entries if slot not in (BENCH, IR))
        pool = [(pos, pts) for slot, pts, pos, _ in entries
                if include_ir or slot != IR]
        sat = [(pts, name) for slot, pts, _, name in entries
               if slot == BENCH or (include_ir and slot == IR)]
        top = max(sat) if sat else (0.0, None)
        optimal = optimal_points(pool, slots)
        out.append({"team_id": team_id, "week": week, "actual": actual,
                    "optimal": optimal, "lost": optimal - actual,
                    "top_bench_name": top[1], "top_bench_points": top[0]})
    return out


def bench_summary(conn: sqlite3.Connection, through_week: Optional[int] = None,
                  include_ir: bool = False) -> Dict[int, Dict[str, Any]]:
    """Season total lost per team, plus that team's worst week."""
    out: Dict[int, Dict[str, Any]] = {}
    for w in bench_weeks(conn, through_week, include_ir):
        s = out.setdefault(w["team_id"], {"total_lost": 0.0, "worst": None})
        s["total_lost"] += w["lost"]
        if s["worst"] is None or w["lost"] > s["worst"]["lost"]:
            s["worst"] = w
    return out


def weekly_scores(conn: sqlite3.Connection, through_week: Optional[int] = None
                  ) -> Dict[int, Dict[int, float]]:
    """{week: {team_id: points}} from ESPN's matchup totals."""
    through_week = through_week or last_completed_week(conn)
    scores: Dict[int, Dict[int, float]] = {}
    for week, h, hp, a, ap in conn.execute(
            """SELECT week, home_team_id, home_points, away_team_id, away_points
               FROM matchups WHERE week <= ?""", (through_week,)):
        for team, pts in ((h, hp), (a, ap)):
            if team is not None:
                scores.setdefault(week, {})[team] = pts or 0.0
    return scores


def records(conn: sqlite3.Connection, through_week: Optional[int] = None
            ) -> Dict[int, List[int]]:
    """Actual head-to-head [wins, losses, ties] per team."""
    through_week = through_week or last_completed_week(conn)
    rec: Dict[int, List[int]] = {}
    for h, hp, a, ap in conn.execute(
            """SELECT home_team_id, home_points, away_team_id, away_points
               FROM matchups WHERE week <= ?""", (through_week,)):
        if h is None or a is None:
            continue
        rh, ra = rec.setdefault(h, [0, 0, 0]), rec.setdefault(a, [0, 0, 0])
        if hp > ap:
            rh[0] += 1; ra[1] += 1
        elif ap > hp:
            ra[0] += 1; rh[1] += 1
        else:
            rh[2] += 1; ra[2] += 1
    return rec


def all_play(conn: sqlite3.Connection, through_week: Optional[int] = None
             ) -> Dict[int, Dict[str, Any]]:
    """Each team vs every other team every week, and the expected wins that implies."""
    out: Dict[int, Dict[str, Any]] = {}
    for week, scores in weekly_scores(conn, through_week).items():
        for team, pts in scores.items():
            s = out.setdefault(team, {"wins": 0, "losses": 0, "ties": 0, "games": 0})
            s["games"] += 1
            for other, opts in scores.items():
                if other == team:
                    continue
                s["wins" if pts > opts else "losses" if pts < opts else "ties"] += 1
    for s in out.values():
        n = s["wins"] + s["losses"] + s["ties"]
        rate = (s["wins"] + 0.5 * s["ties"]) / n if n else 0.0
        s["win_rate"] = rate
        s["expected_wins"] = rate * s["games"]
    return out


# ---------------------------------------------------------------------------
# Dashboard analytics
# ---------------------------------------------------------------------------

POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}
SLOT = {0: "QB", 2: "RB", 4: "WR", 6: "TE", 16: "D/ST", 17: "K", 20: "Bench",
        21: "IR", 23: "FLEX"}


def _players(conn: sqlite3.Connection) -> Dict[int, Dict[str, Any]]:
    cur = conn.execute("SELECT player_id, name, position_id, pro_team_id, on_team_id, "
                       "status, season_points, season_avg, adp FROM players")
    cols = [c[0] for c in cur.description]
    return {r[0]: dict(zip(cols, r)) for r in cur}


def bench_detail(conn: sqlite3.Connection, through_week: Optional[int] = None
                 ) -> Dict[str, Any]:
    """IR-excluded bench report with what-if results.

    For every team-week: the points left on the bench, whether the best lineup
    would have beaten the opponent's real lineup, and whether it would have
    beaten the opponent's own best lineup. Plus the costliest single sit/start
    call each week and whether that one swap would have flipped the game.
    """
    through_week = through_week or last_completed_week(conn)
    slots = starting_slots(conn)
    rows = conn.execute(
        """SELECT l.team_id, l.week, l.slot_id, l.points, p.position_id, p.name
           FROM lineups l JOIN players p USING (player_id)
           WHERE l.week <= ?""", (through_week,)).fetchall()
    grouped: Dict[Tuple[int, int], List[Tuple]] = {}
    for r in rows:
        grouped.setdefault((r[0], r[1]), []).append(r[2:])
    opp: Dict[Tuple[int, int], int] = {}
    for week, h, a in conn.execute(
            "SELECT week, home_team_id, away_team_id FROM matchups WHERE week <= ?",
            (through_week,)):
        if h is not None and a is not None:
            opp[(h, week)], opp[(a, week)] = a, h

    weeks: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for (team_id, week), entries in sorted(grouped.items()):
        starters = [e for e in entries if e[0] not in (BENCH, IR)]
        bench = [e for e in entries if e[0] == BENCH]
        actual = sum(e[1] for e in starters)
        optimal = optimal_points([(e[2], e[1]) for e in starters + bench], slots)
        filled: Dict[int, int] = {}
        for e in starters:
            filled[e[0]] = filled.get(e[0], 0) + 1
        empty = [s for s, n in slots.items() for _ in range(n - filled.get(s, 0))]
        # Costliest single call: a bench player vs the weakest starter he could replace.
        worst = None
        targets = [(e[0], e[1], e[3]) for e in starters] + [(s, 0.0, "(empty slot)") for s in empty]
        for bp in bench:
            options = [t for t in targets if bp[2] in SLOT_POSITIONS.get(t[0], ())]
            if not options:
                continue
            t = min(options, key=lambda x: x[1])
            gain = bp[1] - t[1]
            if gain > 0 and (worst is None or gain > worst["gain"]):
                worst = {"sat": bp[3], "sat_points": bp[1], "started": t[2],
                         "started_points": t[1], "gain": gain}
        weeks[(team_id, week)] = {"team_id": team_id, "week": week, "actual": actual,
                                  "optimal": optimal, "lost": optimal - actual,
                                  "worst_call": worst}

    def outcome(mine: float, theirs: float) -> str:
        return "W" if mine > theirs else "L" if mine < theirs else "T"
    for (team_id, week), w in weeks.items():
        o = weeks.get((opp.get((team_id, week)), week))
        if not o:
            continue
        w["opp_id"] = o["team_id"]
        w["opp_actual"], w["opp_optimal"] = o["actual"], o["optimal"]
        w["result"] = outcome(w["actual"], o["actual"])
        w["perfect_result"] = outcome(w["optimal"], o["actual"])
        w["ideal_result"] = outcome(w["optimal"], o["optimal"])
        w["margin"] = w["actual"] - o["actual"]
        w["perfect_margin"] = w["optimal"] - o["actual"]
        w["ideal_margin"] = w["optimal"] - o["optimal"]
        w["avoidable_loss"] = w["result"] == "L" and w["perfect_result"] == "W"
        if w["worst_call"]:
            w["worst_call"]["flips"] = (w["result"] == "L"
                                        and w["actual"] + w["worst_call"]["gain"] > o["actual"])

    def tally(rec: List[int], res: Optional[str]) -> None:
        if res:
            rec["WLT".index(res)] += 1
    teams: Dict[int, Dict[str, Any]] = {}
    for w in weeks.values():
        t = teams.setdefault(w["team_id"], {
            "team_id": w["team_id"], "total_lost": 0.0, "actual": 0.0, "optimal": 0.0,
            "record": [0, 0, 0], "perfect_record": [0, 0, 0], "ideal_record": [0, 0, 0],
            "avoidable_losses": 0, "one_call_losses": 0})
        t["total_lost"] += w["lost"]
        t["actual"] += w["actual"]
        t["optimal"] += w["optimal"]
        tally(t["record"], w.get("result"))
        tally(t["perfect_record"], w.get("perfect_result"))
        tally(t["ideal_record"], w.get("ideal_result"))
        t["avoidable_losses"] += bool(w.get("avoidable_loss"))
        t["one_call_losses"] += bool(w["worst_call"] and w["worst_call"].get("flips"))
    for t in teams.values():
        t["efficiency"] = t["actual"] / t["optimal"] if t["optimal"] else None
    calls = sorted((dict(w["worst_call"], team_id=w["team_id"], week=w["week"],
                         result=w.get("result"), margin=w.get("margin"),
                         opp_id=w.get("opp_id"))
                    for w in weeks.values() if w["worst_call"]),
                   key=lambda c: -c["gain"])
    flips = [c for c in calls if c.get("flips")]
    return {"weeks": list(weeks.values()),
            "teams": sorted(teams.values(), key=lambda t: -t["total_lost"]),
            "worst_calls": calls[:20], "game_losing_calls": flips}


def schedule_swap(conn: sqlite3.Connection, through_week: Optional[int] = None
                  ) -> Dict[int, Dict[int, int]]:
    """wins[a][b] = team a's wins had it played team b's schedule."""
    through_week = through_week or last_completed_week(conn)
    scores = weekly_scores(conn, through_week)
    opp: Dict[int, Dict[int, int]] = {}
    for week, h, a in conn.execute(
            "SELECT week, home_team_id, away_team_id FROM matchups WHERE week <= ?",
            (through_week,)):
        if h is not None and a is not None:
            opp.setdefault(week, {})[h] = a
            opp[week][a] = h
    teams = sorted({t for w in scores.values() for t in w})
    out: Dict[int, Dict[int, int]] = {a: {b: 0 for b in teams} for a in teams}
    for week, sc in scores.items():
        for a in teams:
            for b in teams:
                o = opp.get(week, {}).get(b)
                if o is None or a not in sc:
                    continue
                if o == a:  # b's opponent that week was a: a plays b instead
                    o = b
                if sc[a] > sc.get(o, 0.0):
                    out[a][b] += 1
    return out


SKILL_SLOT = {1: 0, 2: 2, 3: 4, 4: 6}  # QB, RB, WR, TE -> their lineup slot
_PAST_SEASONS: Dict[int, Dict[str, Any]] = {}


def _season_values(conn: sqlite3.Connection, season: int, through_week: Optional[int] = None
                   ) -> Dict[str, Any]:
    """Points over replacement for every QB, RB, WR and TE in a season.

    The standard value-based measure: a player's season points minus those of
    the replacement-level player at his position, the last starter league-wide
    (QB12, RB30, WR30, TE12 in a 12-team league with this lineup). Kickers and
    defenses are left out.
    """
    current = int(_meta(conn, "season") or 0)
    if season != current and season in _PAST_SEASONS:
        return _PAST_SEASONS[season]
    if season == current:
        last = through_week or last_completed_week(conn)
        position = dict(conn.execute("SELECT player_id, position_id FROM players"))
        picks = conn.execute("SELECT overall_pick, team_id, player_id FROM draft_picks "
                             "ORDER BY overall_pick").fetchall()
    else:
        last = 17  # the fantasy season: regular season plus playoffs
        position = dict(conn.execute(
            "SELECT player_id, position_id FROM history_players WHERE season=?", (season,)))
        picks = conn.execute("SELECT overall_pick, team_id, player_id FROM history_picks "
                             "WHERE season=? ORDER BY overall_pick", (season,)).fetchall()
    points = {pid: pts for pid, pts in conn.execute(
        "SELECT player_id, SUM(points) FROM player_weeks WHERE season=? AND week BETWEEN 1 AND ? "
        "GROUP BY player_id", (season, last)) if position.get(pid) in SKILL_SLOT}
    slots = starting_slots(conn)
    n_teams = len({t for _, t, _ in picks}) or 12
    repl: Dict[int, float] = {}
    repl_rank: Dict[int, int] = {}
    for pos, slot in SKILL_SLOT.items():
        n = n_teams * slots.get(slot, 0) + (n_teams * slots.get(FLEX, 0) // 2 if pos in (2, 3) else 0)
        ranked = sorted((v for pid, v in points.items() if position[pid] == pos), reverse=True)
        repl_rank[pos] = n
        repl[pos] = ranked[min(n, len(ranked)) - 1] if ranked and n else 0.0
    out = {"season": season, "weeks": last, "picks": picks, "points": points,
           "position": position, "repl": repl, "repl_rank": repl_rank, "n_teams": n_teams,
           "raw": {pid: v - repl[position[pid]] for pid, v in points.items()}}
    out["graded"] = {pid for _, _, pid in picks if position.get(pid) in SKILL_SLOT}
    if season != current:
        _PAST_SEASONS[season] = out
    return out


def _over(sv: Dict[str, Any], pid: int) -> float:
    """Points over replacement, floored at zero (a dud is simply replaced)."""
    return max(0.0, sv["raw"].get(pid, 0.0))


def _slot_curve(conn: sqlite3.Connection, through_week: Optional[int] = None) -> List[float]:
    """Expected points over replacement per week for each draft slot.

    The average return of the picks within six spots of a slot, across every
    draft on record. Index 0 is unused; index n is pick n.
    """
    current = int(_meta(conn, "season") or 0)
    seasons = [r[0] for r in conn.execute("SELECT DISTINCT season FROM history_players")]
    samples: Dict[int, List[float]] = {}
    top = 0
    for yr in seasons + [current]:
        sv = _season_values(conn, yr, through_week)
        for overall, _, pid in sv["picks"]:
            top = max(top, overall)
            if pid in sv["graded"]:
                samples.setdefault(overall, []).append(_over(sv, pid) / max(1, sv["weeks"]))
    curve = [0.0] * (top + 1)
    for n in range(1, top + 1):
        near = [v for k in range(n - 6, n + 7) for v in samples.get(k, [])]
        curve[n] = sum(near) / len(near) if near else 0.0
    return curve


def draft_analysis(conn: sqlite3.Connection, through_week: Optional[int] = None
                   ) -> Dict[str, Any]:
    """This season's draft by points over replacement, overall and against
    what each draft slot usually returns."""
    through_week = through_week or last_completed_week(conn)
    season = int(_meta(conn, "season") or 0)
    players = _players(conn)
    sv = _season_values(conn, season, through_week)
    curve = _slot_curve(conn, through_week)
    n_teams, weeks = sv["n_teams"], sv["weeks"]

    by_pos: Dict[int, List[Dict[str, Any]]] = {}
    for p in players.values():
        if p["position_id"] in POS and p["season_points"] is not None:
            by_pos.setdefault(p["position_id"], []).append(p)
    finish: Dict[int, int] = {}
    for plist in by_pos.values():
        plist.sort(key=lambda p: -p["season_points"])
        for i, p in enumerate(plist):
            finish[p["player_id"]] = i + 1

    def nm(pid: Optional[int]) -> str:
        return (players.get(pid) or {}).get("name") or "Player %s" % pid

    def ps(pid: Optional[int]) -> str:
        return POS.get((players.get(pid) or {}).get("position_id"), "?")

    # Consensus order: rank by ESPN ADP rather than the raw number, so league
    # size does not matter. ESPN parks rarely drafted players at about 170.
    real_adp = sorted((p for p in players.values() if p["adp"] and 0 < p["adp"] < 165),
                      key=lambda p: p["adp"])
    adp_rank = {p["player_id"]: i + 1 for i, p in enumerate(real_adp)}
    board = sorted(sv["raw"], key=lambda pid: -sv["raw"][pid])
    redraft_pick = {pid: i + 1 for i, pid in enumerate(board)}

    rounds = dict(conn.execute("SELECT overall_pick, round FROM draft_picks"))
    threshold = 4.0 * max(1, weeks)
    picks = []
    drafted_order: Dict[str, int] = {}
    for overall, team_id, pid in sv["picks"]:
        pos = ps(pid)
        drafted_order[pos] = drafted_order.get(pos, 0) + 1
        graded = pid in sv["graded"]
        over = _over(sv, pid) if graded else None
        expected = curve[overall] * weeks if graded else None
        vs_slot = over - expected if graded else None
        rank = adp_rank.get(pid)
        picks.append({
            "pick": overall, "round": rounds.get(overall), "team_id": team_id,
            "player_id": pid, "name": nm(pid), "pos": pos,
            "pos_drafted": drafted_order[pos], "pos_finish": finish.get(pid),
            "points": sv["points"].get(pid, (players.get(pid) or {}).get("season_points") or 0.0),
            "graded": graded, "over": over, "expected": expected, "vs_slot": vs_slot,
            "label": ("" if not graded else "steal" if vs_slot >= threshold
                      else "bust" if vs_slot <= -threshold else ""),
            "redraft_pick": redraft_pick.get(pid) if graded else None,
            "adp_rank": rank,
            "vs_adp": (overall - rank if rank is not None
                       else min(0, overall - (len(real_adp) + 1)))})

    # Passed over: the best player, any position, taken before this team's next
    # turn. Back-to-back picks share the window after the pair. The last pick has
    # no next turn, so "could have waited" looks at players nobody drafted.
    pick_of = {pid: overall for overall, _, pid in sv["picks"]}
    by_team: Dict[int, List[Dict[str, Any]]] = {}
    for pk in picks:
        by_team.setdefault(pk["team_id"], []).append(pk)
    for pk in picks:
        own = sorted(o["pick"] for o in by_team[pk["team_id"]])
        start = pk["pick"]
        while start + 1 in own:
            start += 1
        later = [o for o in own if o > start]
        end = later[0] if later else 10 ** 6
        window = [o for o in picks if start < o["pick"] < end and o["graded"]
                  and pk["graded"] and o["over"] > pk["over"]]
        best = max(window, key=lambda o: o["over"]) if window else None
        pk["passed"] = best and {"name": best["name"], "pos": best["pos"], "pick": best["pick"],
                                 "team_id": best["team_id"], "over": best["over"],
                                 "gap": best["over"] - pk["over"]}
        # Could have waited: the best player at the same position still on the
        # board at this team's next turn (drafted then or later, or never).
        pk["wait"] = None
        if pk["graded"]:
            pos_id = sv["position"].get(pk["player_id"])
            alt = max((pid for pid in sv["raw"]
                       if sv["position"][pid] == pos_id and pid != pk["player_id"]
                       and pick_of.get(pid, 10 ** 9) >= end),
                      key=lambda pid: sv["raw"][pid], default=None)
            if alt is not None and _over(sv, alt) > 0:
                pk["wait"] = {"name": nm(alt), "pick": pick_of.get(alt),
                              "over": _over(sv, alt), "edge": pk["over"] - _over(sv, alt)}

    teams = []
    for t, tp in by_team.items():
        graded = [p for p in tp if p["graded"]]
        best = max(graded, key=lambda p: p["vs_slot"])
        worst = min(graded, key=lambda p: p["vs_slot"])
        teams.append({
            "team_id": t, "value": sum(p["over"] for p in graded),
            "vs_slot": sum(p["vs_slot"] for p in graded),
            "steals": sum(1 for p in graded if p["label"] == "steal"),
            "busts": sum(1 for p in graded if p["label"] == "bust"),
            "best": {"name": best["name"], "pick": best["pick"], "vs_slot": best["vs_slot"]},
            "worst": {"name": worst["name"], "pick": worst["pick"], "vs_slot": worst["vs_slot"]}})
    teams.sort(key=lambda t: -t["value"])
    for i, t in enumerate(teams):
        t["rank"] = i + 1

    drafted = {pk["player_id"]: pk for pk in picks}
    redraft = [{"redraft_pick": i + 1, "name": nm(pid), "pos": ps(pid),
                "pos_finish": finish.get(pid), "points": sv["points"][pid],
                "over": sv["raw"][pid],
                "actual_pick": (drafted.get(pid) or {}).get("pick"),
                "team_id": (drafted.get(pid) or {}).get("team_id")}
               for i, pid in enumerate(board[:len(picks)])]
    misses = sorted((dict(pk["passed"], team_id=pk["team_id"], took=pk["name"],
                          took_pos=pk["pos"], took_pick=pk["pick"], took_over=pk["over"],
                          taken_by=pk["passed"]["team_id"])
                     for pk in picks if pk["passed"]), key=lambda r: -r["gap"])[:20]
    undrafted = [pid for pid in board if pid not in drafted and sv["raw"][pid] > 0][:15]
    return {"picks": picks, "teams": teams, "redraft": redraft, "misses": misses,
            "replacement": {POS[k]: {"rank": sv["repl_rank"][k], "points": v}
                            for k, v in sv["repl"].items()},
            "weeks": weeks, "label_threshold": threshold,
            "undrafted": [{"name": nm(pid), "pos": ps(pid), "points": sv["points"][pid],
                           "over": sv["raw"][pid],
                           "on_team_id": (players.get(pid) or {}).get("on_team_id"),
                           "pos_finish": finish.get(pid)} for pid in undrafted]}


def _week_starts(conn: sqlite3.Connection) -> Dict[int, int]:
    return dict(conn.execute("SELECT week, MIN(date) FROM pro_games GROUP BY week"))


def trade_ledger(conn: sqlite3.Connection, through_week: Optional[int] = None
                 ) -> List[Dict[str, Any]]:
    """Points scored by each side's incoming players since the trade."""
    through_week = through_week or last_completed_week(conn)
    players = _players(conn)
    starts = _week_starts(conn)
    out = []
    for trade_id, date in conn.execute("SELECT trade_id, date FROM trades ORDER BY date DESC"):
        later = [w for w, d in starts.items() if d and d > date]
        first_week = min(later) if later else through_week + 1
        sides: Dict[int, Dict[str, Any]] = {}
        for pid, frm, to in conn.execute(
                "SELECT player_id, from_team_id, to_team_id FROM trade_items WHERE trade_id=?",
                (trade_id,)):
            pts, started = conn.execute(
                """SELECT COALESCE(SUM(points),0),
                          COALESCE(SUM(CASE WHEN slot_id NOT IN (20,21) AND team_id=? THEN points END),0)
                   FROM lineups WHERE player_id=? AND week BETWEEN ? AND ?""",
                (to, pid, first_week, through_week)).fetchone()
            side = sides.setdefault(to, {"team_id": to, "players": [], "points": 0.0,
                                         "started_points": 0.0})
            side["players"].append({"name": (players.get(pid) or {}).get("name") or str(pid),
                                    "pos": POS.get((players.get(pid) or {}).get("position_id"), "?"),
                                    "points": pts, "started_points": started})
            side["points"] += pts
            side["started_points"] += started
            sides.setdefault(frm, {"team_id": frm, "players": [], "points": 0.0,
                                   "started_points": 0.0})
        out.append({"trade_id": trade_id, "date": date, "first_week": first_week,
                    "weeks_counted": max(0, through_week - first_week + 1),
                    "sides": list(sides.values())})
    return out


def _moves(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Executed adds and drops (waiver + free agent)."""
    out = []
    for week, typ, date, items in conn.execute(
            """SELECT week, type, proposed_date, items FROM transactions
               WHERE type IN ('FREEAGENT','WAIVER') AND status='EXECUTED'
               ORDER BY proposed_date"""):
        adds, drops, team = [], [], None
        for it in json.loads(items or "[]"):
            if it.get("type") == "ADD":
                adds.append(it.get("playerId"))
                team = it.get("toTeamId")
            elif it.get("type") == "DROP":
                drops.append(it.get("playerId"))
                team = team or it.get("fromTeamId")
        if team:
            out.append({"week": week, "type": typ, "date": date, "team_id": team,
                        "adds": adds, "drops": drops})
    return out


def waiver_scores(conn: sqlite3.Connection, through_week: Optional[int] = None
                  ) -> List[Dict[str, Any]]:
    """Points produced, for the adding team, by players picked up in season."""
    through_week = through_week or last_completed_week(conn)
    players = _players(conn)
    first_add: Dict[Tuple[int, int], int] = {}
    adds_count: Dict[int, int] = {}
    for m in _moves(conn):
        for pid in m["adds"]:
            adds_count[m["team_id"]] = adds_count.get(m["team_id"], 0) + 1
            first_add.setdefault((m["team_id"], pid), m["week"])
    teams: Dict[int, Dict[str, Any]] = {}
    for (team_id, pid), week in first_add.items():
        total, started = conn.execute(
            """SELECT COALESCE(SUM(points),0),
                      COALESCE(SUM(CASE WHEN slot_id NOT IN (20,21) THEN points END),0)
               FROM lineups WHERE team_id=? AND player_id=? AND week BETWEEN ? AND ?""",
            (team_id, pid, week, through_week)).fetchone()
        t = teams.setdefault(team_id, {"team_id": team_id, "started_points": 0.0,
                                       "roster_points": 0.0, "best": None})
        t["started_points"] += started
        t["roster_points"] += total
        if started > 0 and (t["best"] is None or started > t["best"]["started_points"]):
            t["best"] = {"name": (players.get(pid) or {}).get("name") or str(pid),
                         "started_points": started, "week": week}
    for team_id, t in teams.items():
        t["adds"] = adds_count.get(team_id, 0)
    return sorted(teams.values(), key=lambda t: -t["started_points"])


def activity(conn: sqlite3.Connection, limit: int = 40) -> List[Dict[str, Any]]:
    players = _players(conn)

    def nm(pid: int) -> str:
        return (players.get(pid) or {}).get("name") or "Player %s" % pid
    feed = [{"date": m["date"], "week": m["week"], "team_id": m["team_id"],
             "kind": "Waiver" if m["type"] == "WAIVER" else "Free agent",
             "adds": [nm(p) for p in m["adds"]], "drops": [nm(p) for p in m["drops"]]}
            for m in _moves(conn)]
    return sorted(feed, key=lambda f: -(f["date"] or 0))[:limit]


def live(conn: sqlite3.Connection, now_ms: int) -> Dict[str, Any]:
    """Current week matchups with per-player status."""
    week = int(_meta(conn, "current_week") or 0)
    games: Dict[int, int] = dict(conn.execute(
        "SELECT pro_team_id, MIN(date) FROM pro_games WHERE week=? GROUP BY pro_team_id",
        (week,)))
    rosters: Dict[int, List[Dict[str, Any]]] = {}
    for team_id, slot, pts, proj, pro, name, pos in conn.execute(
            """SELECT l.team_id, l.slot_id, l.points, l.projected, l.pro_team_id, p.name,
                      p.position_id
               FROM lineups l JOIN players p USING (player_id) WHERE l.week=?""", (week,)):
        kickoff = games.get(pro)
        if kickoff is None:
            state = "bye"
        elif kickoff > now_ms:
            state = "upcoming"
        elif now_ms < kickoff + int(3.75 * 3600 * 1000):
            state = "playing"
        else:
            state = "done"
        rosters.setdefault(team_id, []).append({
            "name": name, "pos": POS.get(pos, "?"), "slot": SLOT.get(slot, str(slot)),
            "slot_id": slot, "points": pts, "projected": proj, "state": state,
            "kickoff": kickoff})
    order = {0: 0, 2: 1, 4: 2, 6: 3, 23: 4, 16: 5, 17: 6, 20: 7, 21: 8}

    def side(team_id: Optional[int], final: float, live_pts: float, proj: float
             ) -> Optional[Dict[str, Any]]:
        if team_id is None:
            return None
        roster = sorted(rosters.get(team_id, []),
                        key=lambda r: (order.get(r["slot_id"], 9), -(r["projected"] or 0)))
        starters = [r for r in roster if r["slot_id"] not in (BENCH, IR)]
        return {"team_id": team_id, "points": live_pts if live_pts else (final or 0.0),
                "projected": proj,
                "yet_to_play": sum(1 for r in starters if r["state"] == "upcoming"),
                "playing": sum(1 for r in starters if r["state"] == "playing"),
                "roster": roster}
    matchups = []
    for h, hp, a, ap, hl, al, hj, aj, winner in conn.execute(
            """SELECT home_team_id, home_points, away_team_id, away_points, home_live,
                      away_live, home_proj, away_proj, winner
               FROM matchups WHERE week=? ORDER BY matchup_id""", (week,)):
        matchups.append({"home": side(h, hp, hl, hj), "away": side(a, ap, al, aj),
                         "winner": winner})
    in_window = any(d <= now_ms < d + int(4.5 * 3600 * 1000) for d in games.values() if d)
    return {"week": week, "matchups": matchups, "games_in_progress": in_window}


def drop_regret(conn: sqlite3.Connection, through_week: Optional[int] = None
                ) -> List[Dict[str, Any]]:
    """Players a manager cut who then produced in someone else's starting lineup,
    set against what that manager actually started in the same weeks.

    For each week the dropped player was started elsewhere, he is compared with
    the weakest starter on the dropping team that he could have replaced (same
    position slot or FLEX; an empty slot counts as zero).
    """
    through_week = through_week or last_completed_week(conn)
    players = _players(conn)
    slots = starting_slots(conn)
    starters: Dict[Tuple[int, int], List[Tuple[int, float]]] = {}
    for team_id, week, slot, pts in conn.execute(
            "SELECT team_id, week, slot_id, points FROM lineups WHERE slot_id NOT IN (20,21)"):
        starters.setdefault((team_id, week), []).append((slot, pts))
    seen = set()
    out = []
    for mv in _moves(conn):
        for pid in mv["drops"]:
            if (mv["team_id"], pid) in seen:
                continue
            seen.add((mv["team_id"], pid))
            p = players.get(pid) or {}
            pos = p.get("position_id")
            rows = conn.execute(
                """SELECT week, team_id, points FROM lineups
                   WHERE player_id=? AND team_id<>? AND slot_id NOT IN (20,21)
                     AND week BETWEEN ? AND ?""",
                (pid, mv["team_id"], mv["week"], through_week)).fetchall()
            if not rows:
                continue
            his = mine = 0.0
            for week, _, pts in rows:
                mine_wk = starters.get((mv["team_id"], week), [])
                options = [x[1] for x in mine_wk if pos in SLOT_POSITIONS.get(x[0], ())]
                want = sum(n for sl, n in slots.items() if pos in SLOT_POSITIONS.get(sl, ()))
                if len(options) < want:
                    options.append(0.0)
                his += pts
                mine += min(options) if options else 0.0
            by_team: Dict[int, float] = {}
            for _, tid, pts in rows:
                by_team[tid] = by_team.get(tid, 0.0) + pts
            out.append({"team_id": mv["team_id"], "week": mv["week"],
                        "name": p.get("name") or str(pid), "pos": POS.get(pos, "?"),
                        "weeks": len(rows), "started_points": his,
                        "own_points": mine, "net": his - mine,
                        "beneficiary_id": max(by_team, key=lambda t: by_team[t])})
    return sorted(out, key=lambda d: -d["net"])


MISSED = ("IR", "PUP", "NFI", "OUT", "DOUBTFUL")  # official statuses that mean he sat
LISTED_ROUNDS = 7  # a lineup has seven QB/RB/WR/TE spots


def _window(conn: sqlite3.Connection, year: int, through_week: int) -> int:
    """Weeks that count for a season: the fantasy season (1 to 17) once finished."""
    return through_week if year == int(_meta(conn, "season") or 0) else 17


def _holders(conn: sqlite3.Connection, year: int) -> Dict[Tuple[int, int], Tuple[int, int]]:
    """(player, week) -> (fantasy team, lineup slot) for one season."""
    if year == int(_meta(conn, "season") or 0):
        rows = conn.execute("SELECT player_id, week, team_id, slot_id FROM lineups")
    else:
        rows = conn.execute("SELECT player_id, week, team_id, slot_id FROM history_rosters "
                            "WHERE season=?", (year,))
    return {(p, w): (t, s) for p, w, t, s in rows}


def _draft_rounds(conn: sqlite3.Connection, year: int) -> Dict[int, Tuple[int, int]]:
    """player -> (team that drafted him, round)."""
    if year == int(_meta(conn, "season") or 0):
        rows = conn.execute("SELECT player_id, team_id, round FROM draft_picks")
    else:
        rows = conn.execute("SELECT player_id, team_id, round FROM history_picks WHERE season=?",
                            (year,))
    return {p: (t, r) for p, t, r in rows}


def injury_stints(conn: sqlite3.Connection, year: int, last: int) -> Dict[int, List[Dict[str, Any]]]:
    """Each player's injuries in a season, from the official weekly statuses.

    An injury starts in the first week he is listed unavailable (MISSED) and runs
    until a week he is available again. Weeks with no listing (byes, or no NFL
    team) neither count nor end it. Only weeks up to `last` are counted.
    """
    status: Dict[int, Dict[int, Tuple[str, str]]] = {}
    for w, pid, st, inj in conn.execute(
            "SELECT week, player_id, status, injury FROM nfl_status WHERE season=? AND week<=? "
            "ORDER BY week", (year, last)):
        status.setdefault(pid, {})[w] = (st, inj or "")
    out: Dict[int, List[Dict[str, Any]]] = {}
    for pid, weeks in status.items():
        cur: Optional[Dict[str, Any]] = None
        for w in sorted(weeks):
            st, inj = weeks[w]
            if st not in MISSED:
                cur = None
                continue
            if cur is None:
                cur = {"onset": w, "weeks": 0, "listed": {}, "injuries": []}
                out.setdefault(pid, []).append(cur)
            cur["weeks"] += 1
            cur["listed"][st] = cur["listed"].get(st, 0) + 1
            if inj:
                cur["injuries"].append(inj)
    for stints in out.values():
        for x in stints:
            inj = x.pop("injuries")
            x["injury"] = max(set(inj), key=inj.count) if inj else ""
    return out


def starters_hurt(conn: sqlite3.Connection, year: int, through_week: int) -> List[Dict[str, Any]]:
    """Injuries charged to the team that had the player when he got hurt.

    The team is whoever had him the week before he was first listed out (the
    drafting team for a week-1 injury). It counts if he had already been in that
    team's starting lineup that season, or, for a week-1 injury, if that team
    drafted him in the first seven rounds. Every week of the injury counts for
    that team, even after a trade or drop.
    """
    last = _window(conn, year, through_week)
    held = _holders(conn, year)
    drafted = _draft_rounds(conn, year)
    pos = dict(conn.execute("SELECT player_id, position_id FROM players"))
    out = []
    for pid, stints in injury_stints(conn, year, last).items():
        if pos.get(pid) not in SKILL_SLOT:
            continue
        for x in stints:
            on = x["onset"]
            if on == 1:
                team, rnd = drafted.get(pid, (None, 99))
                ok = team is not None and rnd <= LISTED_ROUNDS
            else:
                team = (held.get((pid, on - 1)) or (None, None))[0]
                ok = team is not None and any(
                    held.get((pid, w), (None, None))[0] == team
                    and held[(pid, w)][1] not in (BENCH, IR) for w in range(1, on))
            if ok:
                out.append(dict(x, player_id=pid, team_id=team, season=year))
    return out


def trade_injuries(conn: sqlite3.Connection, year: int, through_week: int
                   ) -> List[Dict[str, Any]]:
    """Every trade of a season, with the weeks each side's new players went on to miss.

    Trades are read from the weekly rosters: players moving between two teams
    from one week to the next, in both directions at once. An injury counts for
    the team that received the player if he was still theirs the week before he
    was first listed out (the same rule as starters_hurt), and all its weeks
    count, even after a later trade or drop.
    """
    last = _window(conn, year, through_week)
    held = _holders(conn, year)
    moves: Dict[Tuple[int, int, int], List[int]] = {}  # (week joined, from, to) -> players
    for (pid, w), (team, _) in held.items():
        before = held.get((pid, w - 1))
        if before and before[0] != team:
            moves.setdefault((w, before[0], team), []).append(pid)
    stints = injury_stints(conn, year, last)
    out = []
    for (w, a, b), players in sorted(moves.items()):
        if a > b or (w, b, a) not in moves or w > last:
            continue  # each trade once; one-way moves are drops and pickups
        sides = []
        for team, got in ((b, players), (a, moves[(w, b, a)])):
            items = [{"player_id": pid, "weeks": sum(
                x["weeks"] for x in stints.get(pid, []) if x["onset"] > w
                and held.get((pid, x["onset"] - 1), (None,))[0] == team)} for pid in got]
            sides.append({"team_id": team, "players": items,
                          "weeks": sum(i["weeks"] for i in items)})
        out.append({"season": year, "week": w, "sides": sides})
    return out


def injury_report(conn: sqlite3.Connection, owner_of: Dict[Tuple[int, int], str],
                  team_name: Dict[Tuple[int, int], str], pname: Dict[int, str],
                  seasons: List[int], current: int, through_week: int) -> Dict[str, Any]:
    """Three views of injury luck across every season on record.

    draft: weeks missed by each manager's first LISTED_ROUNDS picks, whoever had
    them by then. starters: starters_hurt, charged to whoever had the player.
    trades: trade_injuries.
    """
    pos = dict(conn.execute("SELECT player_id, position_id FROM players"))

    def player(pid: int) -> Dict[str, Any]:
        return {"name": pname.get(pid) or "Player %s" % pid, "pos": POS.get(pos.get(pid), "?")}

    def table(entries: List[Tuple[int, int, Optional[Dict[str, Any]]]]) -> List[Dict[str, Any]]:
        rows: Dict[str, Dict[str, Any]] = {}
        for yr, tid, item in entries:
            o = owner_of.get((yr, tid))
            if not o:
                continue
            r = rows.setdefault(o, {"owner": o, "by_season": {}, "total": 0})
            cell = r["by_season"].setdefault(yr, {"weeks": 0, "players": []})
            if item:
                cell["weeks"] += item["weeks"]
                cell["players"].append(item)
                r["total"] += item["weeks"]
        for r in rows.values():
            for cell in r["by_season"].values():
                cell["players"].sort(key=lambda x: -x["weeks"])
        return sorted(rows.values(), key=lambda r: -r["total"])

    def longest(entries: List[Tuple[int, int, Optional[Dict[str, Any]]]]) -> List[Dict[str, Any]]:
        top = sorted((e for e in entries if e[2]), key=lambda e: (-e[2]["weeks"], e[0]))[:10]
        return [dict(item, owner=owner_of.get((yr, tid)), season=yr,
                     team=team_name.get((yr, tid))) for yr, tid, item in top]

    draft: List[Tuple[int, int, Optional[Dict[str, Any]]]] = []
    starters: List[Tuple[int, int, Optional[Dict[str, Any]]]] = []
    trades = []
    for yr in seasons:
        last = _window(conn, yr, through_week)
        stints = injury_stints(conn, yr, last)
        for pid, (tid, rnd) in _draft_rounds(conn, yr).items():
            if rnd > LISTED_ROUNDS or pos.get(pid) not in SKILL_SLOT:
                continue
            draft.append((yr, tid, None))  # every drafting team gets a cell, even at zero
            listed: Dict[str, int] = {}
            for x in stints.get(pid, []):
                for k, n in x["listed"].items():
                    listed[k] = listed.get(k, 0) + n
            weeks = sum(listed.values())
            if weeks:
                inj = [x["injury"] for x in stints[pid] if x["injury"]]
                draft.append((yr, tid, dict(player(pid), round=rnd, weeks=weeks, listed=listed,
                                            injury=inj[0] if inj else "")))
        for tid in {t for (y, t) in owner_of if y == yr}:
            starters.append((yr, tid, None))
        for x in starters_hurt(conn, yr, through_week):
            starters.append((yr, x["team_id"], dict(player(x["player_id"]), onset=x["onset"],
                                                    weeks=x["weeks"], listed=x["listed"],
                                                    injury=x["injury"])))
        for t in trade_injuries(conn, yr, through_week):
            trades.append({"season": yr, "week": t["week"], "sides": [
                {"owner": owner_of.get((yr, side["team_id"])),
                 "team": team_name.get((yr, side["team_id"])), "weeks": side["weeks"],
                 "players": [dict(player(i["player_id"]), weeks=i["weeks"])
                             for i in side["players"]]} for side in t["sides"]]})
    trades.sort(key=lambda t: (-t["season"], -t["week"]))
    return {"seasons": seasons, "current": current, "through_week": through_week,
            "rounds": LISTED_ROUNDS,
            "draft": {"rows": table(draft), "longest": longest(draft)},
            "starters": {"rows": table(starters), "longest": longest(starters)},
            "trades": trades}


# League format, read from ESPN settings (see _load_format); these are fallbacks.
REG_WEEKS, BRACKET_SIZE = 14, 8


def _load_format(conn: sqlite3.Connection) -> None:
    """Set the regular-season length and playoff size from the league's settings."""
    global REG_WEEKS, BRACKET_SIZE
    REG_WEEKS = int(_meta(conn, "regular_season_weeks") or REG_WEEKS)
    BRACKET_SIZE = int(_meta(conn, "playoff_team_count") or BRACKET_SIZE)


def _bracket_order(size: int) -> List[int]:
    """Standard seeding for a bracket of `size` (a power of two): 1v8, 4v5, 2v7, 3v6..."""
    order = [1]
    while len(order) < size:
        n = len(order) * 2 + 1
        order = [x for s in order for x in (s, n - s)]
    return order


def _rounds(n: int) -> int:
    r = 0
    while (1 << r) < n:
        r += 1
    return r
SCHEDULE_SAMPLES = 5000
_SCHEDULE_CACHE: Dict[int, Dict[str, Any]] = {}


def _season_games(conn: sqlite3.Connection, year: int, through_week: int
                  ) -> Tuple[Dict[int, Dict[int, float]], Dict[int, Dict[int, int]]]:
    """score[week][team] for every game of a season (playoff and consolation weeks
    included), and opp[week][team] for its regular season."""
    if year == int(_meta(conn, "season") or 0):
        rows = conn.execute("SELECT week, home_team_id, home_points, away_team_id, away_points "
                            "FROM matchups WHERE week <= ?", (through_week,))
    else:
        rows = conn.execute("SELECT week, home_team_id, home_points, away_team_id, away_points "
                            "FROM history_games WHERE season = ?", (year,))
    score: Dict[int, Dict[int, float]] = {}
    opp: Dict[int, Dict[int, int]] = {}
    for w, h, hp, a, ap in rows:
        if h is None or a is None or hp is None or ap is None:
            continue
        score.setdefault(w, {}).update({h: hp, a: ap})
        if w <= REG_WEEKS:
            opp.setdefault(w, {}).update({h: a, a: h})
    return score, opp


def _records(score: Dict[int, Dict[int, float]], opp: Dict[int, Dict[int, int]],
             slot: Dict[int, int]) -> Dict[int, float]:
    """Regular-season wins when each team plays the schedule of slot[team]."""
    holder = {v: k for k, v in slot.items()}
    wins = {t: 0.0 for t in slot}
    for w, pairs in opp.items():
        for t in slot:
            o = holder[pairs[slot[t]]]
            mine, theirs = score[w][t], score[w][o]
            wins[t] += 1.0 if mine > theirs else 0.5 if mine == theirs else 0.0
    return wins


def _champion(score: Dict[int, Dict[int, float]], wins: Dict[int, float],
              points: Dict[int, float]) -> Tuple[int, List[int]]:
    """Seed by wins then points scored, then play a single-elimination bracket of
    BRACKET_SIZE teams with the real playoff-week scores (one round per week,
    standard seeding, top seeds get byes when the field isn't a power of two,
    no reseeding). A tie goes to the higher seed. Returns the champion and the
    seeds in order."""
    seeds = sorted(wins, key=lambda t: (-wins[t], -points[t]))

    def game(week: int, x: Optional[int], y: Optional[int]) -> Optional[int]:
        if x is None or y is None:  # a bye
            return y if x is None else x
        x, y = sorted((x, y), key=seeds.index)
        return x if score[week][x] >= score[week][y] else y
    rounds = _rounds(BRACKET_SIZE)
    field: List[Optional[int]] = [seeds[i - 1] if i <= BRACKET_SIZE else None
                                  for i in _bracket_order(1 << rounds)]
    week = REG_WEEKS + 1
    while len(field) > 1:
        field = [game(week, field[i], field[i + 1]) for i in range(0, len(field), 2)]
        week += 1
    return field[0], seeds  # type: ignore[return-value]


def schedule_season(conn: sqlite3.Connection, year: int, through_week: int) -> Dict[str, Any]:
    """Schedule luck for one season, by team id.

    swap[a][b]: a's wins had a and b traded schedules (a plays b's opponents,
    and b where b played a); swap[a][a] is the real total. For finished seasons
    also: the champion when each pair of teams trades schedules, and how often
    each team makes the playoffs and wins the title across SCHEDULE_SAMPLES
    random schedules (the real schedule with teams dealt into its slots at
    random), always with the real weekly scores.
    """
    _load_format(conn)
    finished = year != int(_meta(conn, "season") or 0)
    if finished and year in _SCHEDULE_CACHE:
        return _SCHEDULE_CACHE[year]
    score, opp = _season_games(conn, year, through_week)
    teams = sorted({t for pairs in opp.values() for t in pairs})
    same = {t: t for t in teams}
    out: Dict[str, Any] = {"swap": {a: {} for a in teams}}
    for a in teams:
        for b in teams:
            out["swap"][a][b] = _records(score, opp, {**same, a: b, b: a})[a]
    playoffs = all(len(score.get(REG_WEEKS + k, {})) == len(teams) for k in range(1, _rounds(BRACKET_SIZE) + 1))
    if finished and playoffs and len(teams) >= BRACKET_SIZE and len(opp) == REG_WEEKS:
        points = {t: sum(score[w][t] for w in opp) for t in teams}
        real, seeds = _champion(score, _records(score, opp, same), points)
        actual = conn.execute("SELECT team_id FROM history_teams WHERE season=? AND final_rank=1",
                              (year,)).fetchone()
        if not actual or actual[0] != real:  # the replay must reproduce what happened
            _SCHEDULE_CACHE[year] = out
            return out
        swap_titles = {t: [] for t in teams}  # partners whose schedule wins t the title
        for i, a in enumerate(teams):
            for b in teams[i + 1:]:
                champ, _ = _champion(score, _records(score, opp, {**same, a: b, b: a}),
                                     points)
                if champ in (a, b) and champ != real:
                    swap_titles[champ].append(b if champ == a else a)
        made = {t: 0 for t in teams}
        won = {t: 0 for t in teams}
        rng = random.Random(year)  # fixed, so the page doesn't change between builds
        for _ in range(SCHEDULE_SAMPLES):
            dealt = teams[:]
            rng.shuffle(dealt)
            champ, order = _champion(score, _records(score, opp, dict(zip(teams, dealt))), points)
            won[champ] += 1
            for t in order[:BRACKET_SIZE]:
                made[t] += 1
        out.update(champion=real, seeds=seeds, swap_titles=swap_titles,
                   playoff_odds={t: made[t] / SCHEDULE_SAMPLES for t in teams},
                   title_odds={t: won[t] / SCHEDULE_SAMPLES for t in teams})
    if finished:
        _SCHEDULE_CACHE[year] = out
    return out


def schedule_luck(conn: sqlite3.Connection, owner_of: Dict[Tuple[int, int], str],
                  seasons: List[int], current: int, through_week: int) -> Dict[str, Any]:
    """schedule_season for every season, keyed by manager instead of team id."""
    swap: Dict[int, Dict[str, Dict[str, float]]] = {}
    rows: Dict[str, Dict[str, Any]] = {}
    title_seasons = []
    for yr in seasons:
        res = schedule_season(conn, yr, through_week)
        o = lambda t: owner_of.get((yr, t))  # noqa: E731
        swap[yr] = {o(a): {o(b): v for b, v in row.items() if o(b)}
                    for a, row in res["swap"].items() if o(a)}
        if "champion" not in res:
            continue
        title_seasons.append(yr)
        for t, seed_at in ((t, res["seeds"].index(t) + 1) for t in res["seeds"]):
            if not o(t):
                continue
            r = rows.setdefault(o(t), {"owner": o(t), "by_season": {}, "titles": 0,
                                       "expected": 0.0})
            r["by_season"][yr] = {"seed": seed_at, "champion": t == res["champion"],
                                  "playoff_odds": res["playoff_odds"][t],
                                  "title_odds": res["title_odds"][t],
                                  "swaps": [o(x) for x in res["swap_titles"][t] if o(x)]}
            r["titles"] += t == res["champion"]
            r["expected"] += res["title_odds"][t]
    return {"seasons": seasons, "current": current, "through_week": through_week,
            "samples": SCHEDULE_SAMPLES, "swap": swap, "title_seasons": title_seasons,
            "titles": sorted(rows.values(), key=lambda r: -r["expected"])}


def history(conn: sqlite3.Connection, through_week: Optional[int] = None) -> Dict[str, Any]:
    """All-time records by manager across the seasons ESPN has."""
    through_week = through_week or last_completed_week(conn)
    season = int(_meta(conn, "season") or 0)
    current = {r[0]: {"team_id": r[1], "name": r[2]} for r in conn.execute(
        "SELECT owner_id, team_id, name FROM teams")}

    def label(owner: str) -> Dict[str, Any]:
        c = current.get(owner)
        return {"owner": owner, "team_id": c["team_id"] if c else None,
                "name": c["name"] if c else None}
    mgr: Dict[str, Dict[str, Any]] = {}
    owner_of: Dict[Tuple[int, int], str] = {}
    team_name: Dict[Tuple[int, int], str] = {}
    for s, tid, owner, name, w, l, t, pf, pa, rank, seed, made in conn.execute(
            "SELECT * FROM history_teams ORDER BY season"):
        owner_of[(s, tid)] = owner
        team_name[(s, tid)] = name
        m = mgr.setdefault(owner, dict(label(owner), seasons=0, wins=0, losses=0, ties=0,
                                       points_for=0.0, titles=0, playoffs=0, finishes={}))
        m["seasons"] += 1
        m["wins"] += w or 0
        m["losses"] += l or 0
        m["ties"] += t or 0
        m["points_for"] += pf or 0.0
        m["titles"] += rank == 1
        m["playoffs"] += made or 0
        m["finishes"][s] = {"rank": rank, "name": name, "record": "%d-%d" % (w or 0, l or 0)}
    for tid, (owner,) in ((r[0], (r[1],)) for r in conn.execute(
            "SELECT team_id, owner_id FROM teams")):
        owner_of[(season, tid)] = owner
        team_name[(season, tid)] = (current.get(owner) or {}).get("name") or ""
        mgr.setdefault(owner, dict(label(owner), seasons=0, wins=0, losses=0, ties=0,
                                   points_for=0.0, titles=0, playoffs=0, finishes={}))
    for m in mgr.values():
        ranks = [f["rank"] for f in m["finishes"].values() if f["rank"]]
        m["avg_finish"] = sum(ranks) / len(ranks) if ranks else None
        g = m["wins"] + m["losses"] + m["ties"]
        m["win_pct"] = (m["wins"] + 0.5 * m["ties"]) / g if g else None

    # Head to head and single-game records: finished seasons plus this year so far.
    games = [(s, w, tier, h, hp, a, ap) for s, w, tier, h, hp, a, ap in conn.execute(
        "SELECT * FROM history_games") if h is not None and a is not None
        and tier in ("NONE", "WINNERS_BRACKET")]
    games += [(season, w, "NONE", h, hp, a, ap) for w, h, hp, a, ap in conn.execute(
        """SELECT week, home_team_id, home_points, away_team_id, away_points FROM matchups
           WHERE week <= ? AND away_team_id IS NOT NULL""", (through_week,))]
    h2h: Dict[str, Dict[str, List[int]]] = {}
    singles = []
    for s, w, tier, h, hp, a, ap in games:
        oh, oa = owner_of.get((s, h)), owner_of.get((s, a))
        if not oh or not oa or hp is None or ap is None or (hp == 0 and ap == 0):
            continue
        for me_, them, mp, tp in ((oh, oa, hp, ap), (oa, oh, ap, hp)):
            rec = h2h.setdefault(me_, {}).setdefault(them, [0, 0, 0])
            rec[0 if mp > tp else 1 if mp < tp else 2] += 1
        for tid, o, p, op, otid in ((h, oh, hp, ap, a), (a, oa, ap, hp, h)):
            singles.append({"season": s, "week": w, "owner": o, "team": team_name.get((s, tid)),
                            "points": p, "opp_points": op, "opp_team": team_name.get((s, otid)),
                            "playoff": tier != "NONE"})
    records = {
        "high": sorted(singles, key=lambda x: -x["points"])[:5],
        "low": sorted(singles, key=lambda x: x["points"])[:5],
        "blowout": sorted(singles, key=lambda x: -(x["points"] - x["opp_points"]))[:5],
        "heartbreak": sorted((x for x in singles if x["points"] < x["opp_points"]),
                             key=lambda x: -x["points"])[:5],
    }

    # Draft tendencies: positions taken in rounds 1-3, and when the first QB goes.
    pos = dict(conn.execute("SELECT player_id, position_id FROM players"))
    pname = dict(conn.execute("SELECT player_id, name FROM players"))
    picks = [(s, rnd, tid, pid) for s, rnd, tid, pid in conn.execute(
        "SELECT season, round, team_id, player_id FROM history_picks")]
    picks += [(season, rnd, tid, pid) for rnd, tid, pid in conn.execute(
        "SELECT round, team_id, player_id FROM draft_picks")]
    tend: Dict[str, Dict[str, Any]] = {}
    for s, rnd, tid, pid in picks:
        o = owner_of.get((s, tid))
        p = POS.get(pos.get(pid))
        if not o or not p:
            continue
        t = tend.setdefault(o, {"early": {}, "first_qb": {}, "first_te": {},
                                "drafts": set(), "taken": {}})
        t["drafts"].add(s)
        t["taken"].setdefault(pid, {})[s] = rnd
        if rnd <= 3:
            t["early"][p] = t["early"].get(p, 0) + 1
        for key, want in (("first_qb", "QB"), ("first_te", "TE")):
            if p == want:
                t[key][s] = min(t[key].get(s, 99), rnd)
    tendencies = []
    for o, t in tend.items():
        avg = lambda d: sum(d.values()) / len(d) if d else None  # noqa: E731
        # Favourites: the same player drafted in two or more seasons.
        favs = sorted(({"name": pname.get(pid) or "Player %s" % pid,
                        "pos": POS.get(pos.get(pid), "?"), "times": len(yrs),
                        "picks": [{"season": y, "round": yrs[y]} for y in sorted(yrs)]}
                       for pid, yrs in t["taken"].items() if len(yrs) >= 2),
                      key=lambda f: (-f["times"], sum(p["round"] for p in f["picks"])))
        tendencies.append(dict(label(o), drafts=len(t["drafts"]), early=t["early"],
                               favorites=favs,
                               first_qb=avg(t["first_qb"]), first_te=avg(t["first_te"])))
    # Draft value season by season (finished seasons use the full year).
    curve = _slot_curve(conn, through_week)
    draft_by: Dict[str, Dict[int, Dict[str, Any]]] = {}
    all_picks: List[Dict[str, Any]] = []
    past = sorted({r[0] for r in conn.execute("SELECT DISTINCT season FROM history_players")})
    for yr in past + [season]:
        sv = _season_values(conn, yr, through_week)
        totals: Dict[int, List[float]] = {}
        info = {} if yr == season else {pid: (name, p) for pid, name, p in conn.execute(
            "SELECT player_id, name, position_id FROM history_players WHERE season=?", (yr,))}
        for overall, tid, pid in sv["picks"]:
            if pid not in sv["graded"]:
                continue
            over = _over(sv, pid)
            vs = over - curve[overall] * sv["weeks"]
            tot = totals.setdefault(tid, [0.0, 0.0])
            tot[0] += over
            tot[1] += vs
            o = owner_of.get((yr, tid))
            if o and yr != season:
                nm_, p_ = info.get(pid) or (pname.get(pid), None)
                all_picks.append({"owner": o, "season": yr, "pick": overall,
                                  "name": nm_ or "Player %s" % pid, "pos": POS.get(p_, "?"),
                                  "over": over, "vs_slot": vs,
                                  "team": team_name.get((yr, tid))})
        for rank, tid in enumerate(sorted(totals, key=lambda t: -totals[t][0])):
            o = owner_of.get((yr, tid))
            if o:
                draft_by.setdefault(o, {})[yr] = {"value": totals[tid][0],
                                                  "vs_slot": totals[tid][1], "rank": rank + 1}
    draft_rows = []
    for o, by in draft_by.items():
        ranks = [v["rank"] for v in by.values()]
        done = [v for y, v in by.items() if y != season]
        draft_rows.append(dict(
            label(o), by_season=by, drafts=len(by), avg_rank=sum(ranks) / len(ranks),
            best_rank=min(ranks), worst_rank=max(ranks),
            finished_value=sum(v["value"] for v in done) if done else None,
            finished_vs_slot=sum(v["vs_slot"] for v in done) if done else None))
    draft_rows.sort(key=lambda r: r["avg_rank"])
    drafting = {"seasons": past + [season], "current": season, "rows": draft_rows,
                "best_picks": [dict(p) for p in sorted(all_picks, key=lambda p: -p["vs_slot"])[:10]],
                "worst_picks": [dict(p) for p in sorted(all_picks, key=lambda p: p["vs_slot"])[:10]]}

    injuries = injury_report(conn, owner_of, team_name, pname,
                             sorted({s for s, _ in owner_of}), season, through_week)
    schedules = schedule_luck(conn, owner_of, sorted({s for s, _ in owner_of}), season,
                              through_week)
    for r in schedules["titles"]:
        r.update(label(r["owner"]))
    for part in (injuries["draft"], injuries["starters"]):
        for r in part["rows"]:
            r.update(label(r["owner"]))

    seasons = sorted({s for s, _ in owner_of if s != season})
    managers = sorted(mgr.values(), key=lambda m: (-(m["titles"]), -(m["win_pct"] or 0)))
    # ESPN member ids stay server-side; the page gets short keys instead.
    key = {m["owner"]: "m%d" % i for i, m in enumerate(managers)}
    for group in (managers, tendencies, draft_rows, drafting["best_picks"],
                  drafting["worst_picks"], injuries["draft"]["rows"],
                  injuries["draft"]["longest"], injuries["starters"]["rows"],
                  injuries["starters"]["longest"], schedules["titles"], *records.values()):
        for row in group:
            if "owner" in row:
                row["key"] = key.get(row.pop("owner"))
    for t in injuries["trades"]:
        for side in t["sides"]:
            side["key"] = key.get(side.pop("owner"))
    for r in schedules["titles"]:
        for cell in r["by_season"].values():
            cell["swaps"] = [key[o] for o in cell["swaps"] if o in key]
    schedules["swap"] = {yr: {key[a]: {key[b]: v for b, v in row.items() if b in key}
                              for a, row in m.items() if a in key}
                         for yr, m in schedules["swap"].items()}
    return {"seasons": seasons, "managers": managers,
            "h2h": {key[a]: {key[b]: r for b, r in row.items() if b in key}
                    for a, row in h2h.items() if a in key},
            "records": records, "drafting": drafting, "injuries": injuries,
            "schedules": schedules,
            "tendencies": sorted(tendencies, key=lambda t: t["first_qb"] or 99)}


def dashboard(conn: sqlite3.Connection, now_ms: int) -> Dict[str, Any]:
    """Everything the web page needs, in one JSON-ready dict."""
    through = last_completed_week(conn)
    teams = {r[0]: {"team_id": r[0], "name": r[1], "abbrev": r[2], "points_for": r[3],
                    "points_against": r[4], "manager": r[5]}
             for r in conn.execute(
                 "SELECT team_id, name, abbrev, points_for, points_against, manager FROM teams")}
    recs, ap = records(conn, through), all_play(conn, through)
    standings = []
    for tid, t in teams.items():
        w, l, ties = recs.get(tid, [0, 0, 0])
        a = ap.get(tid, {"wins": 0, "losses": 0, "ties": 0, "expected_wins": 0.0})
        standings.append({"team_id": tid, "wins": w, "losses": l, "ties": ties,
                          "points_for": t["points_for"], "points_against": t["points_against"],
                          "all_play": "%d-%d" % (a["wins"], a["losses"]),
                          "expected_wins": a["expected_wins"],
                          "luck": w + 0.5 * ties - a["expected_wins"]})
    hurt: Dict[int, int] = {}
    for x in starters_hurt(conn, int(_meta(conn, "season") or 0), through):
        hurt[x["team_id"]] = hurt.get(x["team_id"], 0) + x["weeks"]
    for row in standings:
        row["starter_weeks_hurt"] = hurt.get(row["team_id"], 0)
    standings.sort(key=lambda s: (-(s["wins"] + 0.5 * s["ties"]), -(s["points_for"] or 0)))
    return {
        "league": _meta(conn, "league_name"), "season": _meta(conn, "season"),
        "my_team_id": int(_meta(conn, "my_team_id") or 0) or None,
        "through_week": through, "ingested_at": int(_meta(conn, "ingested_at") or 0),
        "playoff_teams": int(_meta(conn, "playoff_team_count") or 0),
        "teams": teams, "standings": standings,
        "live": live(conn, now_ms),
        "bench": bench_detail(conn, through),
        "draft": draft_analysis(conn, through),
        "schedule_swap": schedule_swap(conn, through),
        "trades": trade_ledger(conn, through),
        "waivers": waiver_scores(conn, through),
        "drop_regret": drop_regret(conn, through),
        "history": history(conn, through),
        "activity": activity(conn),
    }
