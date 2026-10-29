import os
import tempfile
import unittest
import threading
import json
import http.client
import secrets
import time
from pathlib import Path

from preflight.scanner import scan
from preflight.server import validate_rules, save_rules, load_rules, make_handler, SessionLease, stop_when_inactive
import preflight.server as server
from http.server import ThreadingHTTPServer
import build


class ScannerTests(unittest.TestCase):
    def test_custom_phrase_case_insensitive(self):
        found = scan("A cOnFiDeNtIaL launch", [{"name": "Launch", "type": "phrase", "value": "confidential"}])
        self.assertEqual(found[0]["code"], "custom-phrase")
        self.assertNotIn("confidential", found[0]["message"].lower())

    def test_custom_domain_matches_subdomain_not_suffix_trick(self):
        rules = [{"name": "Internal", "type": "domain", "value": "example.org"}]
        found = scan("https://sub.example.org/private\nhttps://notexample.org", rules)
        matches = [f for f in found if f["code"] == "custom-domain"]
        self.assertEqual([f["line"] for f in matches], [1])

    def test_builtin_checks(self):
        found = scan("[[Private Note]]\nhttp://localhost:5000\n<script>bad</script>\nfile:///home/user/private", [])
        codes = {f["code"] for f in found}
        self.assertTrue({"unresolved-link", "unsafe-url", "raw-embed"}.issubset(codes))

    def test_clean_content(self):
        self.assertEqual(scan("## Public title\n[Documentation](https://example.com)", []), [])

    def test_banner_targets_future_updates_page(self):
        html = (Path(__file__).resolve().parents[1] / "preflight" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="https://benjaminjgarcia.com/updates"', html)

    def test_session_lease_expires_without_heartbeat(self):
        lease = SessionLease(timeout=0.1)
        self.assertFalse(lease.expired())
        time.sleep(0.13)
        self.assertTrue(lease.expired())
        lease.touch()
        self.assertFalse(lease.expired())

    def test_inactive_session_shuts_down_server(self):
        token = secrets.token_urlsafe(18)
        lease = SessionLease(timeout=0.1)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(token, lease))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        watcher = threading.Thread(target=stop_when_inactive, args=(httpd, lease), daemon=True)
        thread.start()
        watcher.start()
        try:
            watcher.join(timeout=3)
            thread.join(timeout=3)
            self.assertFalse(watcher.is_alive())
            self.assertFalse(thread.is_alive())
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_heartbeat_requires_origin_and_extends_lease(self):
        token = secrets.token_urlsafe(18)
        lease = SessionLease(timeout=2)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(token, lease))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
            conn.request("POST", f"/s/{token}/api/heartbeat", body="{}", headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
            conn.request("POST", f"/s/{token}/api/heartbeat", body="{}", headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{httpd.server_port}",
            })
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            conn.close()
            self.assertFalse(lease.expired())
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_too_large(self):
        with self.assertRaises(ValueError):
            scan("x" * (1024 * 1024 + 1), [])

    def test_rule_validation(self):
        with self.assertRaises(ValueError):
            validate_rules([{"name": "bad", "type": "domain", "value": "https://example.com"}])

    def test_native_packaging_targets(self):
        self.assertIn("--onefile", build.command("Windows"))
        self.assertEqual(Path(build.command("Darwin")[-1]), build.ROOT / "packaging" / "macos.spec")
        self.assertTrue(Path(build.command("Darwin")[-1]).is_file())
        with self.assertRaises(RuntimeError):
            build.command("Linux")

    def test_rules_are_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = server.APP_DIR
            server.APP_DIR = Path(tmp) / "settings"
            try:
                sample = [{"name": "Company", "type": "phrase", "value": "Secret Project"}]
                save_rules(sample)
                self.assertEqual(load_rules(), sample)
                if os.name != "nt":
                    self.assertEqual(server.rules_path().stat().st_mode & 0o777, 0o600)
            finally:
                server.APP_DIR = original

    def test_loopback_session_and_origin_controls(self):
        token = secrets.token_urlsafe(18)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(token))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            def request(path, method="GET", origin=None):
                conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
                headers = {"Content-Type": "application/json"}
                if origin:
                    headers["Origin"] = origin
                conn.request(method, path, body=json.dumps({"text": "Clean"}) if method == "POST" else None, headers=headers)
                response = conn.getresponse()
                status = response.status
                response.read()
                conn.close()
                return status

            self.assertEqual(request("/s/invalid/api/rules"), 404)
            self.assertEqual(request(f"/s/{token}/"), 200)
            self.assertEqual(request(f"/s/{token}/review.css"), 200)
            self.assertEqual(request(f"/s/{token}/promo-art.svg"), 200)
            self.assertEqual(request(f"/s/{token}/api/scan", "POST", "http://example.com"), 403)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_exit_requires_session_and_origin_and_stops_server(self):
        token = secrets.token_urlsafe(18)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(token))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
            connection.request("POST", f"/s/{token}/api/exit", body="{}", headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{httpd.server_port}",
            })
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_scan_does_not_persist_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = server.APP_DIR
            server.APP_DIR = Path(tmp) / "settings"
            token = secrets.token_urlsafe(18)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(token))
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
                conn.request("POST", f"/s/{token}/api/scan", body=json.dumps({"text": "sensitive document contents"}), headers={
                    "Content-Type": "application/json",
                    "Origin": f"http://127.0.0.1:{httpd.server_port}",
                })
                reply = conn.getresponse()
                self.assertEqual(reply.status, 200)
                reply.read()
                conn.close()
                self.assertFalse((Path(tmp) / "settings").exists())
            finally:
                httpd.shutdown()
                httpd.server_close()
                server.APP_DIR = previous


if __name__ == "__main__":
    unittest.main()
