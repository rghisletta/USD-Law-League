# USD Law League: fantasy football dashboard

A stats site for our ESPN fantasy league, rebuilt automatically during NFL games
and published to GitHub Pages. Based on
[la-liga-del-garage](https://github.com/bryson824/la-liga-del-garage), adapted to
work with any ESPN league: login cookies are optional (public leagues need none),
and team count, regular-season length and playoff size are read from the league's
ESPN settings.

**What's on it:** live matchups, standings with the playoff cut line, points left
on the bench, draft value (steals and busts), luck and all-play records, schedule
swaps, waiver and trade grades, playoff odds, manager history across seasons, and
weekly recaps.

Read-only: it only makes GET requests to ESPN and never changes anything in the league.

## Publish it with GitHub Pages

1. In the repo: **Settings > Pages > Build and deployment > Source = GitHub Actions**.
2. **Settings > Secrets and variables > Actions > Variables** tab, add:
   - `LEAGUE_ID`: the `leagueId=` number from the ESPN league URL
   - `SEASON`: `2026`
   - `MY_TEAM_ID`: your `teamId=` (the page opens on this team)
3. Private league only: on the **Secrets** tab add `ESPN_S2` and `ESPN_SWID`
   (from a browser logged in to ESPN: DevTools > Application > Cookies >
   `https://fantasy.espn.com`).
4. **Actions** tab > "Publish dashboard" > **Run workflow**.

The site appears at `https://rghisletta.github.io/USD-Law-League/`. After that it
rebuilds every 10 minutes during NFL game windows and every 6 hours otherwise.

## Run it on your own computer

Needs Python 3.9+ and nothing else (standard library only).

```
cp .env.example .env      # then fill in LEAGUE_ID and MY_TEAM_ID
python3 server.py         # http://127.0.0.1:8765
python3 build_static.py   # or: write a static copy to site/
```

## Real names

`names.json` maps team IDs to the managers' real names, which the site shows
alongside team names:

```json
{
  "1": {"team": "...", "name": "..."},
  "2": {"team": "...", "name": "..."}
}
```

The `"team"` entry is only a reminder for whoever fills the file in.

## Files

| File | What it does |
|---|---|
| `espn_client.py` | Every ESPN request, with caching (finished weeks are fetched once) |
| `ingest.py` | Turns ESPN's JSON into a SQLite database |
| `analytics.py` | Computes every stat on the page |
| `nfl_client.py` | NFL game schedule and status, for live scoring |
| `server.py` | Local server with live refresh |
| `build_static.py` | Writes `site/index.html` and `site/data.json` for GitHub Pages |
| `static/index.html` | The page itself |
