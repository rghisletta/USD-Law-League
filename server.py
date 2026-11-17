"""Local dashboard server.

    python3 server.py          # this Mac only: http://127.0.0.1:8765
    python3 server.py --lan    # also reachable from phones on the same Wi-Fi

Without --lan it binds to 127.0.0.1 only. With --lan anyone on the same network
can view the dashboard (there is no login), so use it on a network you trust.

Read-only against ESPN: refreshing only re-runs ingest.py's GET requests.
While NFL games are in progress, the current week is re-pulled at most once a
minute (and only while the page is open and polling).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

import analytics
import ingest
from espn_client import EspnClient, EspnError

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "static" / "index.html"
PORT = int(os.environ.get("PORT", "8765"))
LIVE_INTERVAL = 60          # seconds between pulls during games
IDLE_INTERVAL = 3 * 3600    # seconds between pulls otherwise

_lock = threading.Lock()
_state: Dict[str, Any] = {"last_pull": 0.0, "error": None}


def pull(light: bool) -> None:
    """Run an ingest unless one is already running."""
    if not _lock.acquire(blocking=False):
        return
    try:
        ingest.ingest(EspnClient.from_env(), light=light)
        _state["error"] = None
    except EspnError as e:
        _state["error"] = str(e)
    except Exception as e:  # keep serving the last good database
        _state["error"] = "Refresh failed: %s" % e
    finally:
        _state["last_pull"] = time.time()
        _lock.release()


def load() -> Dict[str, Any]:
    conn = sqlite3.connect(str(ingest.DB_PATH))
    try:
        return analytics.dashboard(conn, int(time.time() * 1000))
    finally:
        conn.close()


def data() -> Dict[str, Any]:
    if not ingest.DB_PATH.exists():
        pull(light=False)
        if not ingest.DB_PATH.exists():
            return {"error": _state["error"] or "No data yet."}
    try:
        d = load()
    except sqlite3.OperationalError:
        # The database predates a schema change: rebuild it from the cache.
        pull(light=True)
        d = load()
    age = time.time() - max(_state["last_pull"], d["ingested_at"] / 1000.0)
    live = d["live"]["games_in_progress"]
    if (live and age > LIVE_INTERVAL) or age > IDLE_INTERVAL:
        pull(light=live and age <= IDLE_INTERVAL)
        d = load()
    d["refresh_error"] = _state["error"]
    return d


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/data":
            try:
                self._json(data())
            except Exception as e:
                self._json({"error": "Server error: %s" % e}, 500)
        else:
            self._send(404, b"Not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/api/refresh":
            self._send(404, b"Not found", "text/plain")
            return
        if time.time() - _state["last_pull"] > LIVE_INTERVAL:
            pull(light=False)
        self._json({"ok": _state["error"] is None, "error": _state["error"]})

    def log_message(self, fmt: str, *args: Any) -> None:
        if "/api/data" not in (args[0] if args else ""):
            sys.stderr.write("%s\n" % (fmt % args))


def main() -> None:
    lan = "--lan" in sys.argv
    server = ThreadingHTTPServer(("0.0.0.0" if lan else "127.0.0.1", PORT), Handler)
    print("Dashboard: http://127.0.0.1:%d" % PORT)
    if lan:
        import socket
        print("On your phone (same Wi-Fi): http://%s:%d" % (socket.gethostname(), PORT))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
