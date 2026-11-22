import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import hotfix_probe as hp


class FakeResponse:
    url = hp.OAUTH_TOKEN

    def __init__(self, data):
        self.body = io.BytesIO(data)

    def read(self):
        return self.body.read()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class HotfixTests(unittest.TestCase):
    def test_parser_golden_llama(self):
        source = Path(__file__).resolve().parents[1] / "sample_hotfix.ini"
        records = hp.parse_ini(source.read_text(encoding="utf-8"), "sample_hotfix.ini")
        self.assertEqual(len(records), 4)
        entry = next(x for x in records if x["key"].startswith("3BEAC"))
        self.assertEqual(entry["localizedStrings"]["en"], "Golden Llama Crew")
        self.assertEqual(entry["localizedStrings"]["fr"], "Équipage du lama doré")
        escaped = next(x for x in records if x["key"] == "TestEscapes")
        self.assertIn("\r\n", escaped["localizedStrings"]["en"])

    def test_clear_and_removal(self):
        content = '''+TextReplacements=(Category=Game,Key="a",NativeString="X",LocalizedStrings=(("en","Y")))
+TextReplacements=(Category=Game,Key="b",NativeString="X",LocalizedStrings=(("en","Y")))
-TextReplacements=(Category=Game,Key="a")
'''
        records = hp.parse_ini(content, "Example.ini")
        self.assertEqual([r["key"] for r in records], ["b"])

    def test_client_credentials_oauth_post(self):
        def fake_urlopen(request, timeout):
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.full_url, hp.OAUTH_TOKEN)
            self.assertEqual(request.data, b"grant_type=client_credentials")
            self.assertEqual(request.get_header("Authorization"), "Basic YWJjOnNlY3JldA==")
            return FakeResponse(json.dumps({"access_token": "not_a_real_token", "expires_in": 3600}).encode())

        with patch.object(hp.urllib.request, "urlopen", side_effect=fake_urlopen), redirect_stdout(io.StringIO()):
            token = hp.request_client_credentials_token("abc", "secret", 10)
        self.assertEqual(token, "not_a_real_token")

    def test_automatic_auth_selects_client(self):
        args = Namespace(auth="auto", no_prompt=True, timeout=10)
        with patch.dict(os.environ, {"EPIC_CLIENT_ID": "abc", "EPIC_CLIENT_SECRET": "secret", "EPIC_ACCESS_TOKEN": "old-token"}):
            with patch.object(hp, "request_client_credentials_token", return_value="new-token") as auth:
                self.assertEqual(hp.get_token(args), "new-token")
                auth.assert_called_once_with("abc", "secret", 10)

    def test_manual_token_mode(self):
        args = Namespace(auth="token", no_prompt=True, timeout=10)
        with patch.dict(os.environ, {"EPIC_ACCESS_TOKEN": "bearer manual-token", "EPIC_CLIENT_ID": "abc", "EPIC_CLIENT_SECRET": "secret"}):
            self.assertEqual(hp.get_token(args), "manual-token")

    def test_only_on_change_does_not_rewrite(self):
        records = hp.parse_ini(Path(__file__).resolve().parents[1].joinpath("sample_hotfix.ini").read_text(encoding="utf-8"), "sample_hotfix.ini")
        with tempfile.TemporaryDirectory() as td, redirect_stdout(io.StringIO()):
            target = Path(td)
            hp.save_snapshot(target, records.copy(), [], offline=True, only_on_change=True)
            before = (target / "current.json").read_bytes()
            hp.save_snapshot(target, records.copy(), [], offline=True, only_on_change=True)
            self.assertEqual(before, (target / "current.json").read_bytes())
            self.assertFalse((target / "history.jsonl").exists())
            newer = [dict(item) for item in records]
            newer[0] = {**newer[0], "nativeString": "changed"}
            hp.save_snapshot(target, newer, [], offline=True, only_on_change=True)
            change_data = json.loads((target / "last_changes.json").read_text())
            self.assertEqual(change_data["changeCount"], 1)
            self.assertEqual(change_data["changes"][0]["type"], "modified")


if __name__ == "__main__":
    unittest.main()
