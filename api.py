#!/usr/bin/env python3
"""Simple read-only HTTP API for collected Fortnite localization hotfixes.

Run the collector first, then `python api.py --data-dir data`.
For production, put a proper HTTPS reverse proxy in front of this HTTP server.
No third-party packages required.
"""
from __future__ import annotations

import argparse
import json
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


def make_handler(data_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FortniteHotfixAPI/0.3"

        def send_json(self, status: int, payload: object) -> None:
            body = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Cache-Control", "public, max-age=60")
            self.end_headers()
            self.wfile.write(body)

        def read_json_file(self, name: str):
            try:
                return json.loads((data_dir / name).read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except (OSError, json.JSONDecodeError):
                self.send_json(503, {"error": f"Stored {name} is unreadable"})
                return False

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self):
            parts = urlsplit(self.path)
            path = parts.path.rstrip("/") or "/"
            qs = parse_qs(parts.query)
            if path == "/v1/hotfixes/latest":
                payload = self.read_json_file("last_changes.json")
                if payload is False:
                    return
                return self.send_json(200, payload) if payload is not None else self.send_json(503, {"error": "No change report yet"})

            if path == "/v1/hotfixes/history":
                try:
                    limit = min(max(int(qs.get("limit", ["100"])[0]), 1), 1000)
                except ValueError:
                    return self.send_json(400, {"error": "limit must be an integer"})
                try:
                    with (data_dir / "history.jsonl").open(encoding="utf-8") as f:
                        lines = deque(f, maxlen=limit)
                    history = [json.loads(line) for line in lines if line.strip()]
                except FileNotFoundError:
                    history = []
                except (OSError, json.JSONDecodeError):
                    return self.send_json(503, {"error": "Change history is unreadable"})
                return self.send_json(200, {"limit": limit, "count": len(history), "changes": history})

            snapshot = self.read_json_file("current.json")
            if snapshot is False:
                return
            if snapshot is None:
                return self.send_json(503, {"error": "No hotfix snapshot yet. Run hotfix_probe.py first."})

            if path == "/v1/status":
                return self.send_json(200, {k: snapshot.get(k) for k in (
                    "schemaVersion", "checkedAt", "contentSha256", "count", "offline", "sources"
                )})
            if path == "/v1/hotfixes":
                # Preserves namespace/category to avoid collisions on keys.
                return self.send_json(200, snapshot)
            if path.startswith("/v1/hotfixes/"):
                key = unquote(path[len("/v1/hotfixes/"):])
                records = [r for r in snapshot.get("replacements", []) if r.get("key", "").lower() == key.lower()]
                if "namespace" in qs:
                    records = [r for r in records if r.get("namespace") == qs["namespace"][0]]
                if "category" in qs:
                    records = [r for r in records if r.get("category") == qs["category"][0]]
                return self.send_json(200, {"key": key, "count": len(records), "replacements": records}) if records else self.send_json(404, {"error": "Key not found"})
            return self.send_json(404, {"error": "Not found"})

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve Fortnite localization hotfixes as JSON")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--host", default="127.0.0.1", help="Bind address; default is local only")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with ThreadingHTTPServer((args.host, args.port), make_handler(args.data_dir)) as server:
        print(f"Serving data from {args.data_dir} at http://{args.host}:{args.port}/v1/hotfixes")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping server")


if __name__ == "__main__":
    main()
