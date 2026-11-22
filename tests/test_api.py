import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import api


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.sample = {
            "schemaVersion": 1,
            "checkedAt": "2026-11-22T00:00:00Z",
            "contentSha256": "demo",
            "count": 1,
            "offline": True,
            "sources": [],
            "replacements": [{"key": "HELLO", "namespace": "", "category": "Game", "localizedStrings": {"en": "Test"}}],
        }
        (self.data / "current.json").write_text(json.dumps(self.sample), encoding="utf-8")
        (self.data / "last_changes.json").write_text(json.dumps({"changeCount": 0, "changes": []}), encoding="utf-8")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), api.make_handler(self.data))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def fetch(self, path):
        url = f"http://127.0.0.1:{self.server.server_port}{path}"
        with urllib.request.urlopen(url, timeout=3) as response:
            return json.load(response), response.headers

    def test_snapshot_and_lookup(self):
        body, headers = self.fetch("/v1/hotfixes")
        self.assertEqual(body["count"], 1)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")
        lookup, _ = self.fetch("/v1/hotfixes/HELLO")
        self.assertEqual(lookup["replacements"][0]["localizedStrings"]["en"], "Test")

    def test_status_latest_history(self):
        self.assertEqual(self.fetch("/v1/status")[0]["count"], 1)
        self.assertEqual(self.fetch("/v1/hotfixes/latest")[0]["changeCount"], 0)
        self.assertEqual(self.fetch("/v1/hotfixes/history")[0]["count"], 0)

    def test_missing_key_returns_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.fetch("/v1/hotfixes/NOPE")
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
