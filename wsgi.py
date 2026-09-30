"""WSGI entry point for the read-only dashboard (PythonAnywhere and other WSGI hosts).

The bot itself runs separately (on PythonAnywhere: an always-on task running bot.py);
both use the same trades.db and status.json in this folder.

Local check:  python wsgi.py   -> http://127.0.0.1:8000
"""
from __future__ import annotations

import json

from config import load_config
from dashboard import INDEX_HTML, build_state

_cfg = load_config()

_COMMON_HEADERS = [("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff")]


def application(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    if environ.get("REQUEST_METHOD", "GET") not in ("GET", "HEAD"):
        status, ctype, body = "405 Method Not Allowed", "text/plain", b"read-only"
    elif path in ("/", "/index.html"):
        status, ctype, body = "200 OK", "text/html; charset=utf-8", INDEX_HTML.read_bytes()
    elif path == "/api/state":
        status, ctype, body = "200 OK", "application/json", json.dumps(build_state(_cfg)).encode("utf-8")
    elif path == "/healthz":
        status, ctype, body = "200 OK", "text/plain", b"ok"
    else:
        status, ctype, body = "404 Not Found", "text/plain", b"not found"
    start_response(status, [("Content-Type", ctype), ("Content-Length", str(len(body))), *_COMMON_HEADERS])
    return [] if environ.get("REQUEST_METHOD") == "HEAD" else [body]


if __name__ == "__main__":
    from wsgiref.simple_server import make_server

    with make_server("127.0.0.1", 8000, application) as server:
        print("Dashboard (WSGI) on http://127.0.0.1:8000")
        server.serve_forever()
