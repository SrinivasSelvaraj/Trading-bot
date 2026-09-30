"""WSGI entry point for the dashboard (PythonAnywhere and other WSGI hosts).

The bot itself runs separately (on PythonAnywhere: an always-on task running bot.py);
both use the same trades.db and status.json in this folder. Actions posted here are
queued in the database and carried out by the bot on its next poll.

Local check:  python wsgi.py   -> http://127.0.0.1:8000
"""
from __future__ import annotations

import json

from config import load_config
from dashboard import INDEX_HTML, MAX_BODY_BYTES, build_state, submit_command

_cfg = load_config()

_COMMON_HEADERS = [("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff")]
_REASONS = {200: "OK", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
            405: "Method Not Allowed", 413: "Payload Too Large"}


def application(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET")
    code, ctype, body = 404, "text/plain", b"not found"
    if method == "POST" and path == "/api/command":
        length = int(environ.get("CONTENT_LENGTH") or 0)
        raw = environ["wsgi.input"].read(min(length, MAX_BODY_BYTES + 1))
        code, out = submit_command(_cfg, raw, environ.get("HTTP_X_DASHBOARD_KEY", ""))
        ctype, body = "application/json", json.dumps(out).encode("utf-8")
    elif method not in ("GET", "HEAD"):
        code, body = 405, b"method not allowed"
    elif path in ("/", "/index.html"):
        code, ctype, body = 200, "text/html; charset=utf-8", INDEX_HTML.read_bytes()
    elif path == "/api/state":
        code, ctype, body = 200, "application/json", json.dumps(build_state(_cfg)).encode("utf-8")
    elif path == "/healthz":
        code, body = 200, b"ok"
    start_response(f"{code} {_REASONS.get(code, '')}",
                   [("Content-Type", ctype), ("Content-Length", str(len(body))), *_COMMON_HEADERS])
    return [] if method == "HEAD" else [body]


if __name__ == "__main__":
    from wsgiref.simple_server import make_server

    with make_server("127.0.0.1", 8000, application) as server:
        print("Dashboard (WSGI) on http://127.0.0.1:8000")
        server.serve_forever()
