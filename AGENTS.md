# Project handoff

This is a local, single-account Douyin search-index verification tool. Read README.md before changing it.

## Layout and validation

- `client.py`: fixed HTTPS request, response decoding, strict daily-series parsing.
- `storage.py`: complete-result cache, atomic local output, comparison persistence.
- `app.py`: batch controller, page-value comparison, optional desktop entry point.
- `ui-batch.html`: local control panel. The official webview has no Python API bridge.
- `launcher.py`, `start.command`, `build.command`: macOS launcher and packaging.
- `test_*.py`: synthetic offline fixtures; never require credentials or live network.

Use Python 3.12. In an environment without a desktop:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-core.txt
.venv/bin/python -m unittest -v test_client.py test_storage.py test_app.py
```

Install `requirements.txt` for the desktop app. Real GUI/login behavior has been verified on macOS only. Headless tests do not verify login or online collection. A cloud environment does not inherit the Mac's webview, login session, or local output files.

## Invariants

- Search index is `search_hot_list[].index`; composite index is a separate metric. Never label either as actual search counts.
- Current scope: 1–5 unique keywords, exactly 7 inclusive days, nationwide, aweme. Latest available day must come from the official page.
- Keep TLS validation, the fixed endpoint, no redirects, no automatic retries, and the per-launch single online attempt. Change these only as part of an explicit scope change with an explained design.
- Keep credentials in memory. Do not write, print, commit, upload, or pass real cookies to tests. Clear and close the HTTP session after use.
- Missing data must remain missing, never become zero. Reject duplicate/unexpected keys and dates. Only complete results may be cached.
- A verification pass requires independent official-page observations for every keyword/date. Never copy API values into page observations to claim a match.
- Live login/CAPTCHA steps require the user where appropriate. Do not request extra live data just to run routine tests.

## Repository hygiene

Commit source, tests, and documentation. Keep `.venv`, `dist`, `build`, real `results`, screenshots, browser state, secrets, and historical backups local and ignored. Synthetic fixture values may be committed. Do not add an open-source license on the user's behalf.

The 2026-10-27 live validation covered 5 words for 2026-10-19 through 2026-10-25, with 35/35 exact page matches and a cache hit. The original evidence is local to the validation machine and is not part of this repository. `银杏提取物` was unindexed on the official page; `益生菌` was used instead.
