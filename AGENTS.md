# Project handoff

This is a local, single-account Douyin search-index verification tool. Read README.md before changing it.

The optional cloud PoC is documented in CLOUD_POC.md. The user authorized an
external-device login followed by a direct encrypted session transfer to their
own EC2. This explicit exception does not authorize credential uploads to chat,
MCP arguments, GitHub, logs, arbitrary destinations, or files.

## Layout and validation

- `client.py`: fixed HTTPS request, response decoding, strict daily-series parsing.
- `storage.py`: complete-result cache, atomic local output, comparison persistence.
- `app.py`: batch controller, page-value comparison, optional desktop entry point.
- `ui-batch.html`: local control panel. The official webview has no Python API bridge.
- `launcher.py`, `start.command`, `build.command`: macOS launcher and packaging.
- `test_*.py`: synthetic offline fixtures; never require credentials or live network.
- `cloud_probe.py`: browser-free, stdin-only, one-word/seven-day/one-request EC2 probe.
- `login_transfer.py`: human login on another computer; verified SSH stdin transfer.
- `bootstrap_cloud.sh`: project-local Python/core dependency setup, no sudo/services.

Use Python 3.12. In an environment without a desktop:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-core.txt
.venv/bin/python -m unittest -v test_client.py test_storage.py test_app.py
```

Install `requirements.txt` for the desktop app. Real GUI/login behavior has been verified on macOS only. Headless tests do not verify login or online collection. A cloud environment does not inherit the Mac's webview, login session, or local output files.

## Invariants

Cloud hub scope change authorized 2026-10-28: the user requested a third-party
API and a multi-platform web dashboard on Tencent. With INDEX_HUB_ENABLED=1,
the desktop offers a private owner-only Unix socket to the HTTP task worker.
The worker permits repeated explicit tasks, serializes online attempts, uses a
15-second minimum interval and 50 attempts per Shanghai day (user-authorized trial increase on 2026-10-28), stops on login,
verification, access-denied or rate-limit errors, and does not automatically
retry failed/interrupted tasks. These are engineering limits, not official
Douyin quotas. User expanded cloud dates to past two calendar years (maximum 731 inclusive days) on 2026-10-28. Default remains seven days. Added related/audience modules, one keyword and one online attempt per module; same daily and interval guards. Official static frontend confirmed endpoints/shapes; live responses remain pending validation.
Ordinary desktop launches keep their original single-online-attempt behavior.
2026-10-31 user-authorized scope change: in cloud hub mode browser profiles
(cookies) persist under a 0700 service-owned profile directory; never commit,
upload or print them. INDEX_HUB_PERSIST_LOGIN=0 restores memory-only behavior.
Otherwise cookies remain solely inside the browser process and its per-query Session;
the HTTP API never receives or returns cookies. API keys are separate random
secrets, returned once and stored only as SHA-256 hashes. Do not commit keys,
SQLite state, TLS private keys, real data or browser profiles.

2026-10-31 SMS login scope change: the hub may relay the owner's phone number
and SMS codes in memory only; never log/persist them, mask phone numbers in
responses, and never echo codes. No phone whitelist. Keep the SMS start/send,
pointer/text/code, round/idle deadlines, per-send nonce and consumption guards:
start once per 10s; resend countdown at least 60s; at most 3 sends/hour and
6/Shanghai calendar day (counts only, no numbers); pointer at most 3/s;
text and code each at most 5/round; round at most 10min; code_sent idle at most
5min; at most 3 code submissions/send. Hub scripts/systemd default to sms;
INDEX_HUB_LOGIN_MODE=qr or desktop preserves the fallback, and 8443 is unchanged.
2026-10-31 16:53 user-authorized verification scope change: ordinary gap sliders
only may use a local MIT-licensed library (captcha-recognizer 1.0.2), at most
3 automatic submissions per entire login round across send/login verification.
No paid/third-party solving services or solver network calls. Tests use only
synthetic offline images. Missing dependency/model, low confidence or failure
falls back to manual screenshot input, then remote desktop; rotation goes
directly to remote desktop. Automatic/manual drags share the Qt replayer.
All SMS selectors remain uncalibrated in SMS_SELECTORS until user-assisted
real-page calibration. Screenshot animation alone must not invalidate input.
`fake_sms_page.py` / `test_sms_browser.py` exercise the real Qt/HTTP/Unix bridge
flow only on 127.0.0.1, with synthetic inputs and an off-record browser. Browser
tests require INDEX_HUB_REQUIRE_BROWSER_TESTS=1 under Xvfb; missing optional
solver/model must fail with INDEX_HUB_REQUIRE_SOLVER_TESTS=1. Leak scans cover
test-owned logs (including native Qt/Chromium), temp files, SQLite and responses.
`measure_solver_rss.py --qt` reports app/SMS/model process peak and sampled
process-tree RSS; synthetic page memory is not the deployed cgroup peak.

- Search index is `search_hot_list[].index`; composite index is a separate metric. Never label either as actual search counts.
- Current scope: 1–5 unique keywords, past two calendar years (maximum 731 inclusive days) for cloud (desktop default 7), nationwide, aweme. Latest available day must come from the official page.
- Keep TLS validation, the fixed endpoint, no redirects, no automatic retries, and the per-launch single online attempt. Change these only as part of an explicit scope change with an explained design.
- Keep credentials in memory except for the authorized cloud hub browser profiles above. Do not write, print, commit, upload, or pass real cookies to tests, except for the direct encrypted transfer to the user's verified EC2 endpoint described above. Clear and close the HTTP session after use. The cloud PoC does not persist sessions, refresh login, run a daemon, or validate offline-device operation; those require a separate implementation and validation.
- Missing data must remain missing, never become zero. Reject duplicate/unexpected keys and dates. Only complete results may be cached.
- A verification pass requires independent official-page observations for every keyword/date. Never copy API values into page observations to claim a match.
- Live login/CAPTCHA steps require the user where appropriate. Do not request extra live data just to run routine tests.

## Repository hygiene

Commit source, tests, and documentation. Keep `.venv`, `dist`, `build`, real `results`, screenshots, browser state, secrets, and historical backups local and ignored. Synthetic fixture values may be committed. Do not add an open-source license on the user's behalf.

The 2026-10-27 live validation covered 5 words for 2026-10-19 through 2026-10-25, with 35/35 exact page matches and a cache hit. The original evidence is local to the validation machine and is not part of this repository. `银杏提取物` was unindexed on the official page; `益生菌` was used instead.

2026-10-28 故障修复：关联词及画像请求体需包在 param 对象中，已与官方前端 post 调用核实。云端串行间隔由 60 秒调整为 15 秒试运行，指数任务优先；50 次每日上限及异常暂停保持。平台 HTTP/业务错误码单独展示，空指数任务标记 no_data。
