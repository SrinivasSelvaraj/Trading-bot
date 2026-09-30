"""Deploy the paper bot + dashboard to PythonAnywhere through its API (no console needed).

    export PYTHONANYWHERE_USERNAME=yourname
    export PYTHONANYWHERE_API_TOKEN=...      # Account -> API token
    export PYTHONANYWHERE_HOST=www.pythonanywhere.com   # or eu.pythonanywhere.com
    python deploy_pythonanywhere.py            # add --dry-run to only print the steps

What it does:
1. uploads the project files to /home/<user>/Trading-bot/ (your data files are never touched)
2. creates the web app <user>.pythonanywhere.com if it doesn't exist, and points its WSGI file at wsgi.py
3. reloads the web app
4. creates an always-on task running bot.py (paid accounts only; free accounts get the dashboard
   and are told how to run the bot instead)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
PROJECT_DIR = "Trading-bot"
# Deployment-only or local-only files that don't belong on the server.
SKIP = {"Procfile", ".slugignore", "deploy_heroku.sh", ".python-version", "requirements-dev.txt"}
SKIP_PREFIXES = ("tests/",)


def project_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True).stdout
    return [f for f in out.split() if f not in SKIP and not f.startswith(SKIP_PREFIXES)]


def wsgi_file(home: str) -> str:
    return (
        "# Managed by deploy_pythonanywhere.py\n"
        "import sys\n"
        f"path = '{home}/{PROJECT_DIR}'\n"
        "if path not in sys.path:\n"
        "    sys.path.insert(0, path)\n"
        "from wsgi import application  # noqa: E402,F401\n"
    )


class PA:
    def __init__(self, host: str, user: str, token: str, dry_run: bool):
        self.base = f"https://{host}/api/v0/user/{user}"
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Token {token}"
        self.dry_run = dry_run

    def call(self, method: str, path: str, *, allow: tuple[int, ...] = (404,), **kw) -> requests.Response | None:
        """One API call. Returns None in dry-run; statuses in `allow` are returned instead of aborting."""
        if self.dry_run:
            print(f"  [dry-run] {method} {path}")
            return None
        resp = self.session.request(method, self.base + path, timeout=60, **kw)
        if resp.status_code >= 400 and resp.status_code not in allow:
            raise SystemExit(f"{method} {path} failed: HTTP {resp.status_code} {resp.text[:300]}")
        return resp

    def upload(self, remote_path: str, content: bytes) -> None:
        self.call("POST", f"/files/path{remote_path}", allow=(), files={"content": content})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--python", default="python311", help="PythonAnywhere python version id (default python311)")
    parser.add_argument("--dry-run", action="store_true", help="print the steps without changing anything")
    args = parser.parse_args()

    user = os.environ.get("PYTHONANYWHERE_USERNAME")
    token = os.environ.get("PYTHONANYWHERE_API_TOKEN")
    host = os.environ.get("PYTHONANYWHERE_HOST", "www.pythonanywhere.com")
    if not user or not token:
        print("Set PYTHONANYWHERE_USERNAME and PYTHONANYWHERE_API_TOKEN first.")
        return 2

    pa = PA(host, user, token, args.dry_run)
    home = f"/home/{user}"
    domain = f"{user}.pythonanywhere.com" if host.startswith("www.") else f"{user}.{host.split('.', 1)[0]}.pythonanywhere.com"
    python_bin = args.python.replace("python3", "python3.")  # python311 -> python3.11

    files = project_files()
    print(f"1/4 Uploading {len(files)} files to {home}/{PROJECT_DIR}/")
    for rel in files:
        pa.upload(f"{home}/{PROJECT_DIR}/{rel}", (ROOT / rel).read_bytes())

    print(f"2/4 Web app {domain}")
    existing = pa.call("GET", f"/webapps/{domain}/")
    if existing is None or existing.status_code == 404:
        pa.call("POST", "/webapps/", allow=(), data={"domain_name": domain, "python_version": args.python})
    pa.call("PATCH", f"/webapps/{domain}/", allow=(), data={"force_https": "true"})
    pa.upload(f"/var/www/{domain.replace('.', '_')}_wsgi.py", wsgi_file(home).encode())

    print("3/4 Reloading web app")
    pa.call("POST", f"/webapps/{domain}/reload/", allow=())

    print("4/4 Always-on task for the bot")
    command = f"{python_bin} {home}/{PROJECT_DIR}/bot.py"
    tasks = pa.call("GET", "/always_on/", allow=(403, 404))
    if tasks is not None and tasks.status_code == 200 and any(t.get("command") == command for t in tasks.json()):
        print("  already exists; restart it from the Tasks tab to pick up new code")
    else:
        resp = pa.call("POST", "/always_on/", allow=(400, 403, 404),
                       data={"command": command, "description": "BTC 5m paper bot", "enabled": "true"})
        if resp is not None and resp.status_code >= 400:
            print("  Always-on tasks aren't available on this account (free plan).")
            print(f"  The dashboard works, but to run the bot open a Bash console and run: {command}")
            print("  (free consoles stop after a while; a paid plan keeps the bot running 24/7)")

    print(f"\nDashboard: https://{domain}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
