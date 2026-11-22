# Fortnite Localization Hotfix API

A read-only tracker for Fortnite's **live `TextReplacements` localization hotfixes**, retrieved directly from Epic's Fortnite system cloudstorage. No community-hosted Fortnite data API is required.

This project includes:

- `hotfix_probe.py`: downloads `DefaultGame.ini`, parses active `TextReplacements` and compares snapshots.
- **Automatic client-credentials OAuth** if you supply an **authorized** Epic OAuth client ID and secret.
- **Manual bearer-token fallback** (compatible with the temporary token already tested).
- Current data, last comparison and an append-only history of changes in JSON.
- `api.py`: a local read-only HTTP JSON API to develop the Fortnite Wiki generator integration.
- `.github/workflows/update-hotfixes.yml`: checks twice an hour and commits changes, **but only if the required authorized credentials are configured**.
- A sample INI and unit tests that require no live token.

**Status:** Direct cloudstorage file-list and `DefaultGame.ini` download were verified by the project's tester with a temporary Neptune-supplied client token on November 22, 2026. The live **automated OAuth login has not been verified** because no authorized client secret has been provided. Local OAuth request construction and parser logic have automated tests.

## Requirements

- Python 3.10+ (standard library only, no `pip install` required).
- For live downloads: a legitimately obtained Epic OAuth token that can read Fortnite system cloudstorage, or authorized OAuth client credentials with those permissions.
- To publish: a GitHub account and Git (or upload files through the GitHub web interface).

## Quick test (no token)

Open PowerShell in this folder and run:

```powershell
py -3 hotfix_probe.py --input-ini sample_hotfix.ini --output demo_output
```

The sample includes fictional/demo data only. Real Epic data is **not** included in this repository.

## Option A: Your current manual Neptune-token workflow

```powershell
py -3 hotfix_probe.py --auth token --output data
```

Paste your access token into the hidden prompt and hit Enter. No characters will show while pasting. The script downloads and parses `DefaultGame.ini` from Epic. You can also set `EPIC_ACCESS_TOKEN` in the current process environment instead of using the prompt.

Re-run the command with the same `data` directory later to detect added, modified, and removed entries.

## Option B: Request a fresh OAuth token automatically

**Only if you control or are authorized to use an OAuth client granted Fortnite cloudstorage access.** A client ID, or a Neptune token, **is not enough on its own** to request new client tokens. An ordinary Epic developer client may lack the permissions required for this endpoint.

Interactive local test (prompts for client ID and hides client secret):

```powershell
py -3 hotfix_probe.py --auth client --output data
```

Unattended mode (PowerShell session environment variables, or securely configured deployment secrets):

```powershell
$env:EPIC_CLIENT_ID = 'YOUR_AUTHORIZED_CLIENT_ID'
$env:EPIC_CLIENT_SECRET = 'YOUR_AUTHORIZED_CLIENT_SECRET'
py -3 hotfix_probe.py --auth client --no-prompt --output data --only-on-change
```

The script sends a `client_credentials` request to Epic's official OAuth token endpoint, receives an access token in memory, and uses it to fetch Fortnite system cloudstorage. It **does not save** the secret or bearer token. The example values above are placeholders, not working credentials. Avoid putting actual secrets in PowerShell command history; for real usage, set environment variables through an appropriate secret manager or use the hidden interactive prompt.

To continue manually while seeking authorized credentials, use `--auth token` as in Option A.

### CLI options

```text
--auth auto      (default) use configured OAuth credentials if available,
                 otherwise use EPIC_ACCESS_TOKEN or prompt for a bearer token
--auth client    request a fresh token with authorized client credentials
--auth token     use EPIC_ACCESS_TOKEN or prompt for a token
--no-prompt      fail instead of waiting for interactive input (for CI)
--only-on-change don't rewrite outputs if the hotfix dataset hasn't changed
--list           enumerate Epic cloudstorage file names
--files ...      request specific INI filenames, defaults to DefaultGame.ini
--all-ini        try every .ini file (some may be inaccessible)
--input-ini ...  parse local INI instead of connecting to Epic
--output ...     output directory, defaults to hotfix_output
--save-raw       optionally save copies of downloaded INI files
```

## Output files

- `data/current.json` — latest active snapshot with namespace, category, key, native string and localized strings.
- `data/last_changes.json` — previous comparison (baseline on first run).
- `data/history.jsonl` — append-only detected events after an initial snapshot.

With `--only-on-change`, unchanged output files are not rewritten. The snapshot's `checkedAt` time then means **when the stored snapshot last changed**, not the timestamp of every polling attempt. GitHub Actions logs show successful checks when the data did not change.

**Safety:** If a download fails, the collector does not overwrite the existing snapshot. An empty or malformed hotfix file is rejected. First run establishes a baseline and does not count all entries as newly added.

## Run the local HTTP API

First generate `data/current.json`, then:

```powershell
py -3 api.py --data-dir data
```

Open another PowerShell terminal, or your browser, and visit:

- `http://127.0.0.1:8765/v1/hotfixes` — current snapshot.
- `http://127.0.0.1:8765/v1/hotfixes/latest` — last observed change batch.
- `http://127.0.0.1:8765/v1/hotfixes/history?limit=100` — recent history.
- `http://127.0.0.1:8765/v1/hotfixes/<KEY>` — entries matching a localization key.
- `http://127.0.0.1:8765/v1/status` — snapshot metadata.

The server binds to **localhost** by default. GitHub repositories and GitHub Pages do not run this Python HTTP server; to make it a public REST API, deploy it to a server or serverless hosting platform with HTTPS. Alternatively, the generated JSON files can be consumed as a static read-only data feed from GitHub's raw content URLs.

## Publish the project to GitHub

1. On GitHub, create a **new empty repository** named `fortnite-localization-hotfix-api` (do not initialize with README or `.gitignore` because both are included).
2. In PowerShell, navigate into this extracted project folder and run:

```powershell
git init
git branch -M main
git add .
git commit -m "Initial Fortnite hotfix collector"
git remote add origin https://github.com/YOUR_USERNAME/fortnite-localization-hotfix-api.git
git push -u origin main
```

Replace `YOUR_USERNAME` with your account. If Git isn't installed, install Git for Windows or use GitHub's web upload interface. **Don't commit secrets, temporary tokens, or files containing credentials.** The `.gitignore` excludes `.env` and common local output directories, but inspect `git status` before publishing.

### Enable automated GitHub Actions checks (requires authorized credentials)

Under the repository's **Settings → Secrets and variables → Actions**, create these repository secrets:

- `EPIC_CLIENT_ID`
- `EPIC_CLIENT_SECRET`

Then open **Actions → Collect localization hotfixes → Run workflow**. The workflow checks twice per hour and commits `data/current.json`, `data/last_changes.json`, and `data/history.jsonl` **only when the dataset changes**. Without both secrets, it deliberately skips the download, and the repository will not auto-refresh.

The workflow requests `contents: write`. If its push is rejected by repository permissions or branch protection, adjust the repository's Actions workflow permissions or use a PR-based update process. Never put a secret directly into the YAML.

Once the first snapshot is committed, a raw JSON feed is available at:

```text
https://raw.githubusercontent.com/YOUR_USERNAME/fortnite-localization-hotfix-api/main/data/current.json
```

That link is a **static JSON feed**, not a separately hosted API service. GitHub Pages can be configured later, but commits made with GitHub Actions' `GITHUB_TOKEN` **don't automatically trigger a GitHub Pages build**; a dedicated Pages deployment workflow is required for reliably updated Pages content.

## Run tests

```powershell
py -3 -m unittest discover -s tests -v
```

## Future Fortnite Wiki generator integration

The generator currently uses exported localization data. When the collector is deployed, add a small client-side lookup that uses the **localization key plus namespace/category when relevant**, never just the old English text (e.g. `Blank`), to apply active replacements to cosmetic names, descriptions and translations. Keep the last confirmed snapshot when Epic's source is temporarily unreachable.

## Credentials and usage notes

The Epic OAuth `client_credentials` grant does **not** provide a refresh token. Repeated authorized client-credentials requests replace expiring access tokens. The project does **not** include any internal Epic client secret, attempt to extract Neptune's credentials, or circumvent Epic access restrictions. Epic controls whether a given client may read the files, and access can change.
