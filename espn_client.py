"""All ESPN fantasy API access lives here.

Read-only by construction: the only HTTP verb this module issues is GET, against
the league "reads" host. There is no code path that writes to the account.

Responses are cached as JSON under cache/<season>/ so completed weeks are
fetched once. Large player payloads are slimmed before caching.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"


class EspnError(Exception):
    pass


class EspnAuthError(EspnError):
    """Cookies missing, expired, or not allowed to see the league."""


def load_env(path: Optional[Path] = None) -> Dict[str, str]:
    """Minimal .env parser (KEY=VALUE lines, # comments, optional quotes)."""
    path = path or ROOT / ".env"
    keys = ("ESPN_S2", "ESPN_SWID", "LEAGUE_ID", "SEASON", "MY_TEAM_ID")
    # Environment variables are the fallback (used by the GitHub Action).
    env: Dict[str, str] = {k: os.environ[k] for k in keys if os.environ.get(k)}
    if not path.exists():
        if env.get("LEAGUE_ID"):
            return env
        raise EspnError("No .env file found at %s (copy .env.example and fill it in)" % path)
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key.strip()] = value
    return env


def _slim_player(p: Dict[str, Any], season: int) -> Dict[str, Any]:
    pl = p.get("player") or {}
    total = avg = None
    weekly: Dict[str, float] = {}
    for s in pl.get("stats") or []:
        if s.get("seasonId") != season or s.get("statSourceId") != 0:
            continue
        if s.get("statSplitTypeId") == 0:
            total, avg = s.get("appliedTotal"), s.get("appliedAverage")
        elif s.get("statSplitTypeId") == 1 and s.get("scoringPeriodId"):
            weekly[str(s["scoringPeriodId"])] = s.get("appliedTotal") or 0.0
    own = pl.get("ownership") or {}
    return {
        "id": p.get("id"), "name": (pl.get("fullName") or "").strip(),
        "positionId": pl.get("defaultPositionId"), "proTeamId": pl.get("proTeamId"),
        "onTeamId": p.get("onTeamId"), "status": p.get("status"),
        "injuryStatus": pl.get("injuryStatus"),
        "seasonPoints": total, "seasonAvg": avg, "weekly": weekly,
        "adp": own.get("averageDraftPosition"), "pctOwned": own.get("percentOwned"),
    }


class EspnClient:
    def __init__(self, league_id: str, season: int, espn_s2: str, swid: str,
                 my_team_id: Optional[int] = None, cache_dir: Optional[Path] = None):
        if not league_id:
            raise EspnError("LEAGUE_ID must be set (in .env or the environment)")
        # Cookies are only needed for private leagues; public leagues read without them.
        if swid and not (swid.startswith("{") and swid.endswith("}")):
            swid = "{" + swid.strip("{}") + "}"
        self.league_id = str(league_id)
        self.season = int(season)
        self.my_team_id = my_team_id
        self._cookie = "espn_s2=%s; SWID=%s" % (espn_s2, swid) if espn_s2 and swid else ""
        self.cache_dir = (cache_dir or ROOT / "cache") / str(self.season)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls, path: Optional[Path] = None) -> "EspnClient":
        env = load_env(path)
        mine = env.get("MY_TEAM_ID", "")
        return cls(
            league_id=env.get("LEAGUE_ID", ""),
            season=int(env.get("SEASON", "0") or 0),
            espn_s2=env.get("ESPN_S2", ""),
            swid=env.get("ESPN_SWID", ""),
            my_team_id=int(mine) if mine.isdigit() else None,
        )

    # ---- low level -------------------------------------------------------

    def _url(self, params: List[Tuple[str, Any]], league: bool, suffix: str,
             season: Optional[int] = None) -> str:
        path = "/segments/0/leagues/%s" % self.league_id if league else ""
        return "%s/seasons/%d%s%s?%s" % (
            BASE, season or self.season, path, suffix, urllib.parse.urlencode(params))

    def _fetch(self, params: List[Tuple[str, Any]], league: bool = True, suffix: str = "",
               fantasy_filter: Optional[Dict[str, Any]] = None,
               season: Optional[int] = None) -> Any:
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (fantasy-dashboard; local)",
        }
        if self._cookie:
            headers["Cookie"] = self._cookie
        if fantasy_filter is not None:
            headers["x-fantasy-filter"] = json.dumps(fantasy_filter)
        req = urllib.request.Request(self._url(params, league, suffix, season), method="GET",
                                     headers=headers)
        last: Optional[Exception] = None
        for attempt in range(4):
            if attempt:
                time.sleep(2 ** attempt)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = resp.read().decode("utf-8")
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if e.code in (401, 403) or "AUTH_LEAGUE_NOT_VISIBLE" in detail:
                    raise EspnAuthError(
                        "ESPN refused access to the league (HTTP %d). If the league is "
                        "private, set ESPN_S2 / ESPN_SWID (or refresh them)." % e.code)
                if e.code == 429 or e.code >= 500:
                    last = e
                    continue
                raise EspnError("ESPN returned HTTP %d: %s" % (e.code, detail))
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                continue
            try:
                return json.loads(body)
            except ValueError:
                # A login page instead of JSON also means bad cookies.
                raise EspnAuthError("ESPN did not return JSON; check LEAGUE_ID (and cookies, if the league is private).")
        raise EspnError("ESPN request failed after retries: %s" % last)

    def _read(self, name: str) -> Any:
        path = self.cache_dir / (name + ".json")
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def _write(self, name: str, data: Any) -> None:
        tmp = self.cache_dir / (name + ".json.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.cache_dir / (name + ".json"))

    def get(self, name: str, views: List[str], scoring_period: Optional[int] = None,
            refresh: bool = False, league: bool = True, suffix: str = "",
            fantasy_filter: Optional[Dict[str, Any]] = None,
            transform: Optional[Callable[[Any], Any]] = None) -> Any:
        """Fetch views, caching the JSON as cache/<season>/<name>.json."""
        if not refresh:
            cached = self._read(name)
            if cached is not None:
                return cached
        params: List[Tuple[str, Any]] = [("view", v) for v in views]
        if scoring_period is not None:
            params.append(("scoringPeriodId", scoring_period))
        data = self._fetch(params, league=league, suffix=suffix,
                           fantasy_filter=fantasy_filter)
        if transform:
            data = transform(data)
        self._write(name, data)
        return data

    # ---- endpoints -------------------------------------------------------

    def league(self, refresh: bool = True) -> Any:
        return self.get("league", ["mSettings", "mTeam", "mStatus"], refresh=refresh)

    def matchups(self, week: int, refresh: bool = False) -> Any:
        return self.get("matchups_wk%02d" % week, ["mMatchup", "mMatchupScore"],
                        scoring_period=week, refresh=refresh)

    def draft(self, refresh: bool = False) -> Any:
        return self.get("draft", ["mDraftDetail"], refresh=refresh)

    def transactions(self, week: int, refresh: bool = False) -> Any:
        return self.get("transactions_wk%02d" % week, ["mTransactions2"],
                        scoring_period=week, refresh=refresh)

    @staticmethod
    def _top_filter(year: int, limit: int) -> Dict[str, Any]:
        """Top scorers of a season, with every week's score attached."""
        return {"players": {
            "limit": limit,
            "sortAppliedStatTotal": {"sortAsc": False, "sortPriority": 1,
                                     "value": "00%d" % year},
            "filterStatsForTopScoringPeriodIds": {
                "value": 18, "additionalValue": ["00%d" % year, "10%d" % year]}}}

    def players_top(self, limit: int = 500, refresh: bool = False) -> List[Dict[str, Any]]:
        """Top scorers this season (rostered or not) with weekly scores, slimmed."""
        return self.get(
            "players_weekly", ["kona_player_info"], refresh=refresh,
            fantasy_filter=self._top_filter(self.season, limit),
            transform=lambda d: [_slim_player(p, self.season) for p in d.get("players", [])])

    def players_by_ids(self, ids: Iterable[int], refresh: bool = False
                       ) -> List[Dict[str, Any]]:
        """Specific players by id, slimmed. Only ids missing from cache are fetched."""
        ids = sorted(set(int(i) for i in ids))
        cached: Dict[str, Any] = {} if refresh else (self._read("players_extra") or {})
        missing = [i for i in ids if str(i) not in cached]
        for start in range(0, len(missing), 150):
            chunk = missing[start:start + 150]
            data = self._fetch([("view", "kona_player_info")],
                               fantasy_filter={"players": {"filterIds": {"value": chunk}}})
            for p in data.get("players", []):
                cached[str(p.get("id"))] = _slim_player(p, self.season)
            for i in chunk:  # remember ids ESPN does not know, so we do not re-ask
                cached.setdefault(str(i), {"id": i, "name": "", "positionId": None})
        if missing:
            self._write("players_extra", cached)
        return [cached[str(i)] for i in ids]

    def trades(self, refresh: bool = False) -> List[Dict[str, Any]]:
        """Executed trades from the league activity feed."""
        flt = {"topics": {
            "filterType": {"value": ["ACTIVITY_TRANSACTIONS"]},
            "limit": 200, "limitPerMessageSet": {"value": 25}, "offset": 0,
            "sortMessageDate": {"sortPriority": 1, "sortAsc": False},
            "sortFor": {"sortPriority": 2, "sortAsc": False},
            "filterIncludeMessageTypeIds": {"value": [244]}}}

        def slim(d: Any) -> List[Dict[str, Any]]:
            return [{"id": t.get("id"), "date": t.get("date"),
                     "items": [{"playerId": m.get("targetId"), "from": m.get("from"),
                                "to": m.get("to")}
                               for m in t.get("messages", [])
                               if m.get("messageTypeId") == 244]}
                    for t in d.get("topics", [])]
        return self.get("trades", ["kona_league_communication"], refresh=refresh,
                        suffix="/communication/", fantasy_filter=flt, transform=slim)

    def pro_schedule(self, refresh: bool = False) -> List[Dict[str, Any]]:
        """NFL teams with bye week and kickoff times per scoring period."""
        def slim(d: Any) -> List[Dict[str, Any]]:
            return [{"id": t.get("id"), "abbrev": t.get("abbrev"),
                     "byeWeek": t.get("byeWeek"),
                     "games": {wk: [g.get("date") for g in games]
                               for wk, games in (t.get("proGamesByScoringPeriod") or {}).items()}}
                    for t in (d.get("settings") or {}).get("proTeams", [])]
        return self.get("pro_schedule", ["proTeamSchedules_wl"], refresh=refresh,
                        league=False, transform=slim)

    def history(self, year: int, refresh: bool = False) -> Dict[str, Any]:
        """A finished earlier season: teams, final ranks, every game, the draft."""
        name = "history_%d" % year
        if not refresh:
            cached = self._read(name)
            if cached is not None:
                return cached
        d = self._fetch([("view", v) for v in ("mSettings", "mTeam", "mStatus",
                                               "mMatchupScore", "mDraftDetail")], season=year)
        sched = (d.get("settings") or {}).get("scheduleSettings") or {}
        out = {
            "season": year,
            "playoffTeamCount": sched.get("playoffTeamCount"),
            "regularWeeks": sched.get("matchupPeriodCount"),
            "members": [{"id": m.get("id"), "firstName": m.get("firstName"),
                         "lastName": m.get("lastName")} for m in d.get("members", [])],
            "teams": [{"id": t.get("id"), "name": (t.get("name") or "").strip(),
                       "owner": t.get("primaryOwner"),
                       "record": (t.get("record") or {}).get("overall") or {},
                       "finalRank": t.get("rankCalculatedFinal"),
                       "playoffSeed": t.get("playoffSeed")} for t in d.get("teams", [])],
            "games": [{"week": g.get("matchupPeriodId"), "tier": g.get("playoffTierType"),
                       "home": (g.get("home") or {}).get("teamId"),
                       "homePoints": (g.get("home") or {}).get("totalPoints"),
                       "away": (g.get("away") or {}).get("teamId"),
                       "awayPoints": (g.get("away") or {}).get("totalPoints")}
                      for g in d.get("schedule", [])],
            "picks": [{"overall": p.get("overallPickNumber"), "round": p.get("roundId"),
                       "teamId": p.get("teamId"), "playerId": p.get("playerId")}
                      for p in (d.get("draftDetail") or {}).get("picks", [])],
        }
        self._write(name, out)
        return out

    def week_rosters(self, year: int, week: int) -> List[List[int]]:
        """Every team's roster in one week of a finished season, as
        [team id, player id, lineup slot]. Cached for good."""
        name = "rosters_%d_wk%02d" % (year, week)
        cached = self._read(name)
        if cached is not None:
            return cached
        d = self._fetch([("view", "mRoster"), ("scoringPeriodId", week)], season=year)
        out = [[t.get("id"), e.get("playerId"), e.get("lineupSlotId")]
               for t in d.get("teams", []) for e in (t.get("roster") or {}).get("entries", [])]
        self._write(name, out)
        return out

    def players_season(self, year: int, limit: int = 500) -> List[Dict[str, Any]]:
        """Top scorers of a finished earlier season, slimmed. Cached for good."""
        name = "players_weekly_%d" % year
        cached = self._read(name)
        if cached is not None:
            return cached
        d = self._fetch([("view", "kona_player_info")],
                        fantasy_filter=self._top_filter(year, limit), season=year)
        out = [_slim_player(p, year) for p in d.get("players", [])]
        self._write(name, out)
        return out
