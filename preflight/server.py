"""Loopback-only local application server; no third-party dependencies."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .scanner import MAX_TEXT, scan

APP_DIR = Path(os.environ.get("PREFLIGHT_CONFIG_DIR", Path.home() / ".publication-preflight"))
STATIC_DIR = Path(__file__).parent / "static"
MAX_REQUEST = MAX_TEXT * 4
SESSION_TIMEOUT_SECONDS = 60


class SessionLease:
    """Tracks only last client activity; never stores document content."""

    def __init__(self, timeout=SESSION_TIMEOUT_SECONDS):
        self.timeout = timeout
        self.last_seen = time.monotonic()
        self.lock = threading.Lock()

    def touch(self):
        with self.lock:
            self.last_seen = time.monotonic()

    def expired(self):
        with self.lock:
            return time.monotonic() - self.last_seen >= self.timeout


def stop_when_inactive(httpd, lease):
    """Stop the loopback service after the browser stops checking in."""
    while not lease.expired():
        time.sleep(min(2, max(0.05, lease.timeout / 4)))
    httpd.shutdown()



def validate_rules(rules):
    if not isinstance(rules, list) or len(rules) > 200:
        raise ValueError("Rules must be a list of at most 200 items")
    result = []
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"name", "type", "value"}:
            raise ValueError("Each rule needs name, type and value")
        name, kind, value = rule["name"], rule["type"], rule["value"]
        if not all(isinstance(v, str) for v in (name, kind, value)) or kind not in ("phrase", "domain"):
            raise ValueError("Invalid rule fields")
        if not (1 <= len(name.strip()) <= 70 and 1 <= len(value.strip()) <= 200):
            raise ValueError("Rule name or value is outside limits")
        if kind == "domain":
            value = value.strip().lower().rstrip(".")
            if ("/" in value or ":" in value or " " in value or "." not in value or
                    any(not label or not all(c.isalnum() or c == "-" for c in label) for label in value.split("."))):
                raise ValueError("Enter a hostname, for example internal.example.com")
        result.append({"name": name.strip(), "type": kind, "value": value.strip()})
    return result


def rules_path():
    return APP_DIR / "rules.json"


def load_rules():
    path = rules_path()
    if not path.exists():
        return []
    return validate_rules(json.loads(path.read_text(encoding="utf-8")))


def save_rules(rules):
    rules = validate_rules(rules)
    APP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(APP_DIR, 0o700)
    path = rules_path()
    temp = APP_DIR / (".rules-" + secrets.token_hex(6))
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rules, f, ensure_ascii=False, indent=2)
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        temp.unlink(missing_ok=True)
    return rules


def make_handler(token, lease=None):
    prefix = "/s/" + token

    class Handler(BaseHTTPRequestHandler):
        server_version = "PreflightLocal"

        def log_message(self, format, *args):
            pass  # Avoid logging private URLs, rules or scan content.

        def _authorized(self):
            host = self.headers.get("Host", "")
            return host == f"127.0.0.1:{self.server.server_port}" and self.path.split("?", 1)[0].startswith(prefix + "/")

        def _headers(self, status, mime, length):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            self.end_headers()

        def _reply(self, status, data, mime="application/json; charset=utf-8"):
            raw = json.dumps(data).encode() if mime.startswith("application/json") else data
            self._headers(status, mime, len(raw))
            self.wfile.write(raw)

        def do_GET(self):
            if not self._authorized():
                self._reply(404, {"error": "Not found"})
                return
            if lease is not None:
                lease.touch()
            subpath = self.path.split("?", 1)[0][len(prefix):]
            if subpath in ("/", "/index.html", "/app.js", "/style.css", "/review.css", "/promo-art.svg"):
                name = "index.html" if subpath == "/" else subpath[1:]
                mime = ("text/html; charset=utf-8" if name.endswith(".html") else
                        "text/javascript; charset=utf-8" if name.endswith(".js") else
                        "text/css; charset=utf-8" if name.endswith(".css") else "image/svg+xml")
                self._reply(200, (STATIC_DIR / name).read_bytes(), mime)
            elif subpath == "/api/rules":
                try:
                    self._reply(200, {"rules": load_rules()})
                except (OSError, ValueError, json.JSONDecodeError):
                    self._reply(500, {"error": "Saved rules are invalid; scans are disabled until corrected"})
            else:
                self._reply(404, {"error": "Not found"})

        def do_POST(self):
            if not self._authorized() or self.headers.get("Origin") != f"http://127.0.0.1:{self.server.server_port}":
                self._reply(403, {"error": "Forbidden"})
                return
            subpath = self.path.split("?", 1)[0][len(prefix):]
            if subpath not in ("/api/rules", "/api/scan", "/api/exit", "/api/heartbeat") or self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                self._reply(404, {"error": "Not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if length < 0 or length > MAX_REQUEST:
                    raise ValueError("Request is too large or malformed")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("Expected JSON object")
                if lease is not None:
                    lease.touch()
                if subpath == "/api/heartbeat":
                    if body:
                        raise ValueError("Heartbeat request must be empty")
                    self._reply(200, {"status": "active"})
                elif subpath == "/api/exit":
                    if body:
                        raise ValueError("Exit request must be empty")
                    self._reply(200, {"status": "stopping"})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                elif subpath == "/api/rules":
                    self._reply(200, {"rules": save_rules(body.get("rules"))})
                else:
                    current = load_rules()  # Fail closed if persisted settings are corrupt.
                    findings = scan(body.get("text"), current)
                    self._reply(200, {"findings": findings, "passed": not findings, "rules_count": len(current)})
            except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
                self._reply(400, {"error": str(exc)})

    return Handler


def main():
    token = secrets.token_urlsafe(24)
    lease = SessionLease()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(token, lease))
    url = f"http://127.0.0.1:{server.server_port}/s/{token}/"
    print("Publication Preflight running locally. It stops after 60 seconds without browser activity.")
    threading.Thread(target=stop_when_inactive, args=(server, lease), daemon=True).start()
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

