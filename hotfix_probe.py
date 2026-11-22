#!/usr/bin/env python3
"""Track Fortnite TextReplacements directly from Epic cloudstorage.

Supports pre-issued bearer tokens and authorized OAuth client-credentials
logins. No third-party data API, and no secrets are stored in output files.
Python 3.10+, standard library only.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CLOUDSTORAGE = (
    "https://fngw-mcp-gc-livefn.ol.epicgames.com/fortnite/api/cloudstorage/system"
)
OAUTH_TOKEN = "https://account-public-service-prod.ol.epicgames.com/account/api/oauth/token"
ENTRY_START = re.compile(r"(?m)^\s*([+\-]?)TextReplacements\s*=\s*\(", re.IGNORECASE)
CLEAR_START = re.compile(r"(?m)^\s*!TextReplacements\s*=\s*ClearArray\b", re.IGNORECASE)
LANG_PAIR = re.compile(r'\(\s*"((?:\\.|[^"\\])*)"\s*,\s*"((?:\\.|[^"\\])*)"\s*\)', re.S)


class ProbeError(Exception):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def decode_unreal_quoted(text: str) -> str:
    """Decode common escapes while preserving unrecognized Unreal backslashes."""
    table = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
    out = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            next_char = text[i + 1]
            if next_char in table:
                out.append(table[next_char])
                i += 2
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def split_top_level(value: str) -> list[str]:
    """Split on commas outside nested parentheses and quoted strings."""
    pieces = []
    start = 0
    depth = 0
    quoted = False
    escaped = False
    for i, char in enumerate(value):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            pieces.append(value[start:i].strip())
            start = i + 1
    pieces.append(value[start:].strip())
    return pieces


def matching_parenthesis(text: str, start: int) -> int:
    """Return the closing paren index, ignoring quoted parentheses."""
    depth = 0
    quoted = False
    escaped = False
    for i in range(start, len(text)):
        char = text[i]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i
    raise ProbeError("Unterminated TextReplacements expression")


def read_property(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        return decode_unreal_quoted(raw[1:-1])
    return raw


def parse_entry(raw: str, filename: str) -> dict:
    # Split regular properties safely; LocalizedStrings contains nested pairs.
    properties: dict[str, str] = {}
    for piece in split_top_level(raw):
        if "=" not in piece:
            continue
        key, value = piece.split("=", 1)
        properties[key.strip()] = value.strip()

    key = read_property(properties.get("Key", ""))
    if not key:
        raise ProbeError(f"Found TextReplacements with no Key in {filename}")

    loc_raw = properties.get("LocalizedStrings", "")
    localized = {
        decode_unreal_quoted(lang): decode_unreal_quoted(value)
        for lang, value in LANG_PAIR.findall(loc_raw)
    }

    # Some older configurations have malformed parenthesis placement; look for
    # pairs throughout the expression if localized pairs weren't picked up.
    if not localized and "LocalizedStrings" in properties:
        loc_start = raw.find("LocalizedStrings=")
        if loc_start >= 0:
            localized = {
                decode_unreal_quoted(lang): decode_unreal_quoted(value)
                for lang, value in LANG_PAIR.findall(raw[loc_start:])
            }

    if "LocalizedStrings" in properties and not localized and '"' in loc_raw:
        raise ProbeError(f"Cannot parse LocalizedStrings for {key} in {filename}")

    return {
        "sourceFile": filename,
        "category": read_property(properties.get("Category", "")),
        "namespace": read_property(properties.get("Namespace", "")),
        "key": key,
        "nativeCulture": read_property(properties.get("NativeCulture", "")),
        "nativeString": read_property(properties.get("NativeString", "")),
        "isMinimalPatch": read_property(properties.get("bIsMinimalPatch", "")).lower() == "true",
        "localizedStrings": localized,
    }


def parse_ini(content: str, filename: str) -> list[dict]:
    """Parse additions/removals in input order, applying ClearArray directives."""
    actions = [(m.start(), "clear", None) for m in CLEAR_START.finditer(content)]
    actions += [(m.start(), m.group(1) or "+", m) for m in ENTRY_START.finditer(content)]
    actions.sort(key=lambda action: action[0])

    entries: dict[tuple[str, str, str, str], dict] = {}
    for _, operation, match in actions:
        if operation == "clear":
            entries.clear()
            continue
        assert match is not None
        open_paren = match.end() - 1
        close_paren = matching_parenthesis(content, open_paren)
        record = parse_entry(content[open_paren + 1:close_paren], filename)
        identity = (
            record["sourceFile"], record["category"], record["namespace"], record["key"]
        )
        if operation == "-":
            entries.pop(identity, None)
        else:
            entries[identity] = record

    return sorted(entries.values(), key=lambda r: (
        r["sourceFile"].lower(), r["category"].lower(), r["namespace"], r["key"]
    ))


def get_bytes(url: str, token: str | None, timeout: float) -> bytes:
    """Fetch HTTPS data. Never forward bearer tokens to HTTP redirects/CDNs."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise ProbeError("Refusing a non-HTTPS URL")
    headers = {
        "Accept": "*/*",
        "User-Agent": "FortniteLocalizationHotfixAPI/0.3",
    }
    request = urllib.request.Request(url, headers=headers)
    if token:
        # Only the first request receives this header. urllib will NOT forward
        # unredirected headers to a redirected CDN or another domain.
        request.add_unredirected_header("Authorization", "bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if urllib.parse.urlsplit(response.url).scheme != "https":
                raise ProbeError("Refusing a non-HTTPS redirect")
            return response.read()
    except urllib.error.HTTPError as exc:
        # Epic's structured error is important: 400 does NOT imply bad auth.
        raw = exc.read(8192).decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            code = str(payload.get("errorCode") or "").strip()
            message = str(payload.get("errorMessage") or "").strip()
            hint = ": ".join(v for v in (code, message) if v)
        else:
            hint = re.sub(r"\s+", " ", raw).strip()[:250]
        # Avoid accidental credential disclosure in any unexpected error body.
        if token and hint:
            hint = hint.replace(token, "[REDACTED]")
        hint = hint[:400]
        raise ProbeError(f"HTTP {exc.code}" + (f" — {hint}" if hint else "")) from None
    except urllib.error.URLError as exc:
        raise ProbeError(f"Network error contacting Epic: {exc.reason}") from None


def download_cloudstorage_file(file_info: dict, token: str, timeout: float) -> bytes:
    """Try the file endpoint, then Epic's /links readLink download method.

    Epic has changed cloudstorage backends over time, so we try both the
    `uniqueFilename` from the listing and the human-readable `filename`.
    """
    unique = str(file_info["uniqueFilename"])
    filename = str(file_info["filename"])
    identifiers = list(dict.fromkeys([unique, filename]))
    attempts: list[str] = []

    # Direct resource endpoint is supported by the documented cloudstorage API.
    for identifier in identifiers:
        label = "uniqueFilename" if identifier == unique else "filename"
        url = CLOUDSTORAGE + "/" + urllib.parse.quote(identifier, safe="")
        try:
            data = get_bytes(url, token, timeout)
            print(f"{filename}: downloaded using direct {label} ({len(data)} bytes)")
            return data
        except ProbeError as exc:
            attempts.append(f"direct {label}: {exc}")
            print(f"{filename}: direct {label} failed: {exc}")

    # Newer storage backends can return a temporary authenticated readLink,
    # which is then fetched without forwarding the Epic OAuth bearer token.
    for identifier in identifiers:
        label = "uniqueFilename" if identifier == unique else "filename"
        url = CLOUDSTORAGE + "/" + urllib.parse.quote(identifier, safe="") + "/links"
        try:
            response = json.loads(get_bytes(url, token, timeout).decode("utf-8-sig"))
            link = response.get("readLink") if isinstance(response, dict) else None
            if not isinstance(link, str) or not link.startswith("https://"):
                raise ProbeError("No valid HTTPS readLink in Epic's response")
            data = get_bytes(link, None, timeout)
            print(f"{filename}: downloaded using {label} readLink ({len(data)} bytes)")
            return data
        except (ProbeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            attempts.append(f"{label} /links: {exc}")
            print(f"{filename}: {label} /links failed: {exc}")

    raise ProbeError(
        f"Unable to download {filename} using any documented cloudstorage method. "
        "See the error details above. The file list succeeded, so the token "
        "is at least authorized to enumerate files."
    )


def decode_ini(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ProbeError("Hotfix file is not UTF-8 or UTF-16 text") from None


def request_client_credentials_token(client_id: str, client_secret: str, timeout: float) -> str:
    """Get a short-lived token using an AUTHORIZED Epic OAuth client.

    Epic-specific service permissions are attached to the OAuth client;
    regular Epic Developer Portal credentials may lack cloudstorage READ.
    """
    if not client_id or not client_secret:
        raise ProbeError("Client authentication requires both EPIC_CLIENT_ID and EPIC_CLIENT_SECRET")
    credentials = base64.b64encode((client_id + ":" + client_secret).encode("utf-8")).decode("ascii")
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("ascii")
    req = urllib.request.Request(
        OAUTH_TOKEN,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "FortniteLocalizationHotfixAPI/0.3",
        },
    )
    # Basic Authorization must never be forwarded to a redirected host.
    req.add_unredirected_header("Authorization", "Basic " + credentials)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if urllib.parse.urlsplit(response.url).scheme != "https":
                raise ProbeError("OAuth authentication redirected to an insecure URL")
            payload = json.loads(response.read().decode("utf-8-sig"))
    except urllib.error.HTTPError as exc:
        # Never print the credential pair, Basic header, or full response.
        try:
            error_data = json.loads(exc.read(4096).decode("utf-8", "replace"))
            code = error_data.get("errorCode") or error_data.get("error") or ""
        except (ValueError, AttributeError):
            code = ""
        if isinstance(code, str):
            code = code.replace(client_secret, "[REDACTED]").replace(client_id, "[CLIENT]")[:100]
        else:
            code = ""
        raise ProbeError(f"Epic OAuth returned HTTP {exc.code}" + (f" ({code})" if code else "")) from None
    except urllib.error.URLError as exc:
        raise ProbeError(f"Epic OAuth connection failed: {exc.reason}") from None
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProbeError("Epic OAuth returned invalid JSON") from None

    if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str) or not payload["access_token"]:
        raise ProbeError("Epic OAuth response did not include an access token")
    expiry = payload.get("expires_in")
    print("Epic OAuth client authentication succeeded" + (f" (expires in {expiry} seconds)" if isinstance(expiry, int) else ""))
    return payload["access_token"]


def get_token(args: argparse.Namespace) -> str:
    """Choose automatic client auth, supplied access token, or hidden prompt."""
    client_id = os.getenv("EPIC_CLIENT_ID", "").strip()
    client_secret = os.getenv("EPIC_CLIENT_SECRET", "").strip()
    access_token = os.getenv("EPIC_ACCESS_TOKEN", "").strip()

    use_client = args.auth == "client" or (
        args.auth == "auto" and (bool(client_id) or bool(client_secret))
    )
    if use_client:
        if not client_id and not args.no_prompt:
            client_id = input("Authorized Epic OAuth client ID: ").strip()
        if not client_secret and not args.no_prompt:
            client_secret = getpass.getpass("Epic OAuth client secret (hidden): ").strip()
        return request_client_credentials_token(client_id, client_secret, args.timeout)

    if not access_token and not args.no_prompt:
        access_token = getpass.getpass("Epic access token (input hidden): ").strip()
    if access_token.lower().startswith("bearer "):
        access_token = access_token[7:].strip()
    if not access_token:
        raise ProbeError(
            "No credentials available. Set EPIC_CLIENT_ID + EPIC_CLIENT_SECRET "
            "for automatic OAuth, or EPIC_ACCESS_TOKEN, or omit --no-prompt."
        )
    return access_token


def load_remote(args: argparse.Namespace) -> tuple[list[dict], list[dict]]:
    token = get_token(args)
    raw = get_bytes(CLOUDSTORAGE, token, args.timeout)
    try:
        listing = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProbeError("Epic file-list response is not JSON") from None
    if not isinstance(listing, list):
        raise ProbeError("Unexpected Epic file-list response (expected array)")
    files = [f for f in listing if isinstance(f, dict) and f.get("filename") and f.get("uniqueFilename")]
    print(f"Epic returned {len(files)} system cloudstorage files")

    if args.list:
        for f in sorted(files, key=lambda f: f["filename"].lower()):
            print(f'{f["filename"]}  updated={f.get("uploaded", "unknown")}')
        return [], []

    wanted = [name.strip().lower() for name in args.files.split(",") if name.strip()]
    selected = [f for f in files if f["filename"].lower() in wanted]
    if args.all_ini:
        selected = [f for f in files if f["filename"].lower().endswith(".ini")]
    missing = sorted(set(wanted) - {f["filename"].lower() for f in selected}) if not args.all_ini else []
    if missing:
        raise ProbeError(f"Epic file list does not include: {', '.join(missing)}")
    if not selected:
        raise ProbeError("No matching INI files returned by Epic")

    records = []
    metadata = []
    for file_info in sorted(selected, key=lambda f: f["filename"].lower()):
        name = file_info["filename"]
        ini = decode_ini(download_cloudstorage_file(file_info, token, args.timeout))
        parsed = parse_ini(ini, name)
        print(f"{name}: {len(parsed)} active text replacements")
        records.extend(parsed)
        metadata.append({
            "filename": name,
            "hash": file_info.get("hash256") or file_info.get("hash"),
            "uploaded": file_info.get("uploaded"),
            "records": len(parsed),
        })
        if args.save_raw:
            raw_dir = args.output / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / Path(name).name).write_text(ini, encoding="utf-8")

    return records, metadata


def record_identity(record: dict) -> str:
    # Key alone may be shared between namespaces or files.
    return json.dumps([record["sourceFile"], record["category"],
                       record["namespace"], record["key"]], ensure_ascii=False)


def save_snapshot(output: Path, records: list[dict], sources: list[dict], offline: bool, only_on_change: bool = False) -> None:
    output.mkdir(parents=True, exist_ok=True)
    snapshot_path = output / "current.json"
    changes_path = output / "last_changes.json"
    history_path = output / "history.jsonl"
    timestamp = utc_now()

    records.sort(key=lambda r: (r["sourceFile"], r["category"], r["namespace"], r["key"]))
    digest = hashlib.sha256(json.dumps(records, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    previous = None
    if snapshot_path.exists():
        try:
            previous = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise ProbeError(f"Existing {snapshot_path} is unreadable: {exc}") from None

    # Never replace a production snapshot with an offline fixture accidentally.
    if previous is not None and previous.get("offline") != offline:
        raise ProbeError("Offline/live mode mismatch with existing snapshot. Use a different --output directory")
    if only_on_change and previous is not None and digest == previous.get("contentSha256"):
        print("No hotfix changes; existing JSON files left untouched (no Git commit needed).")
        return
    previous_records = previous.get("replacements", []) if previous else []
    old = {record_identity(record): record for record in previous_records}
    new = {record_identity(record): record for record in records}
    changes = []
    if previous is not None:
        for id_value in sorted(set(old) | set(new)):
            if id_value not in old:
                changes.append({"type": "added", "current": new[id_value]})
            elif id_value not in new:
                changes.append({"type": "removed", "previous": old[id_value]})
            elif old[id_value] != new[id_value]:
                changes.append({"type": "modified", "previous": old[id_value], "current": new[id_value]})

    snapshot = {
        "schemaVersion": 1,
        "offline": offline,
        "checkedAt": timestamp,
        "contentSha256": digest,
        "count": len(records),
        "sources": sources,
        "replacements": records,
    }
    change_report = {
        "checkedAt": timestamp,
        "baseline": previous is None,
        "changeCount": len(changes),
        "changes": changes,
    }
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    changes_path.write_text(json.dumps(change_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if changes:
        with history_path.open("a", encoding="utf-8") as stream:
            for change in changes:
                stream.write(json.dumps({"observedAt": timestamp, **change}, ensure_ascii=False) + "\n")
    print(f"Saved {len(records)} replacements to {snapshot_path}")
    print("First snapshot (baseline)." if previous is None else f"Detected {len(changes)} change(s).")
    if changes:
        print(f"History appended to {history_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fortnite localization hotfix collector")
    parser.add_argument("--input-ini", type=Path, help="Parse an existing INI locally (no token needed)")
    parser.add_argument("--list", action="store_true", help="List Epic system files, don't download content")
    parser.add_argument("--files", default="DefaultGame.ini", help="Comma-separated cloudstorage filenames")
    parser.add_argument("--all-ini", action="store_true", help="Download all .ini files (some may be restricted)")
    parser.add_argument("--output", type=Path, default=Path("hotfix_output"))
    parser.add_argument("--save-raw", action="store_true", help="Also save downloaded raw .ini files")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--auth", choices=("auto", "client", "token"), default="auto",
                        help="auto: use OAuth env credentials when present, else bearer token")
    parser.add_argument("--no-prompt", action="store_true", help="Fail instead of asking for credentials (CI use)")
    parser.add_argument("--only-on-change", action="store_true",
                        help="Keep output files unchanged when no replacements changed (GitHub Actions)")
    args = parser.parse_args()

    try:
        if args.input_ini:
            ini = args.input_ini.read_text(encoding="utf-8-sig")
            records = parse_ini(ini, args.input_ini.name)
            sources = [{"filename": args.input_ini.name, "records": len(records)}]
            print(f"Local test: parsed {len(records)} entries")
        else:
            records, sources = load_remote(args)
            if args.list:
                return 0
        if not records:
            raise ProbeError("Zero text replacements found. Refusing to replace snapshot; verify input")
        save_snapshot(args.output, records, sources, offline=bool(args.input_ini), only_on_change=args.only_on_change)
        return 0
    except (ProbeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
