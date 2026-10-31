# 拆分服务模板

systemd/ 下两个 unit 仅作模板，不自动安装；desktop 启动桌面和应用，gateway 仅启动 HTTPS 网关，重启网关不影响桌面。
模板按完整方案写：服务账号 douyin-svc，状态 /var/lib/douyin-index，日志 /var/log/douyin-index；代码目录和 .runtime 只读，desktop 只额外可写 .cloud-home 和 results。不要设置 SuccessExitStatus=143（脚本在子进程被 SIGTERM 时以 143 退出，需要靠它触发 Restart=on-failure）。
INDEX_HUB_STATE_DIR 默认项目下 .cloud-state；两服务须配置同一目录以共享 socket、SQLite、access.json 和证书。
INDEX_HUB_LOG_DIR 默认项目下 logs；Python 日志只写轮转文件（INDEX_HUB_LOG_STDERR=1 才复制到 stderr），进程输出追加到 *.stdout.log，由 deploy/logrotate/douyin-index 轮转。
INDEX_HUB_BIND 默认 0.0.0.0，INDEX_HUB_PORT 默认 8443（1–65535）。
启动桌面前，以服务账号运行 x11vnc -storepasswd，将口令保存到状态目录的 vnc.passwd 并 chmod 600；缺失或权限过宽会拒绝启动。

### 实验扫码登录

`INDEX_HUB_LOGIN_MODE=desktop`（默认）沿用远程桌面登录；设为 `qr` 启用接入页扫码卡片与管理员扫码接口。其他值退回 desktop 并仅警告一次。QR 模式按需启动浏览器，无 QR 会话时仍可使用远程桌面；安全验证需回远程桌面处理。

`INDEX_HUB_QR_TIMEOUT` 设置每轮扫码超时秒数，默认 180；无效、非正或非有限值使用默认值。交接后的会话只保存在 `app.py` 进程内存中，不写状态文件和日志，进程重启后内存交接会话清空，可从持久浏览器资料重新登录。此原型尚需真实页面校准，不改变取数保护限制。

`INDEX_HUB_QR_BROWSER=headed|headless` 设置扫码浏览器模式，默认 `headed`，复用桌面单元导出的 `DISPLAY`；缺少 DISPLAY 时拒绝启动，不回退无头模式。`ProtectSystem=strict` 下 profile 目录必须在 `ReadWritePaths` 内；浏览器程序目录通过 `PLAYWRIGHT_BROWSERS_PATH` 只读即可。

交接后的会话只在 `app.py` 进程内存中，不写状态文件和日志。扫码 Chromium 默认使用服务账号所有的 0700 持久 profile，重启后保留登录资料；`INDEX_HUB_PERSIST_LOGIN=0` 时恢复 Playwright 临时 profile（PrivateTmp 下，正常关闭删除，强杀残留直到单元停止）。Chromium 沙箱由 Playwright 默认关闭（`--no-sandbox`），代码显式设置 `chromium_sandbox=False`；开启需另做 AppArmor/userns 配置。

真实浏览器的 UA 原样交给后续取数，不伪造；headed 模式若检测到 `HeadlessChrome` 会拒绝该会话。Playwright 默认带 `navigator.webdriver=true`（有头、无头相同），本项目不做隐藏。

浏览器由 root 装到 `/opt/ms-playwright`（root 所有、`a+rX`），单元模板已设 `PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright`；Python 依赖装进项目 `.venv`：`.venv/bin/pip install -r requirements-qr.txt`。离线自检（不访问网络、不碰凭证）：`DISPLAY=:N PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright .venv/bin/python qr_browser_check.py headed`，退出码 0 且 `headless_ua=false` 为通过。

云端 Hub 默认持久化浏览器登录资料到服务账号所有的 0700 目录：`INDEX_HUB_PROFILE_DIR`，默认 `INDEX_HUB_STATE_DIR/browser-profiles`。Cookie 以近似明文形式保存在该目录（无系统钥匙串），任何能以 douyin-svc 或 root 身份读盘的人都能拿到登录态；若目录不归 douyin-svc 所有或是符号链接，app.py 会拒绝启动。禁止提交、上传或打印这些资料。`INDEX_HUB_PERSIST_LOGIN=0` 恢复无痕模式；普通桌面启动仍使用无痕模式。远程桌面先由管理员携带 `X-Hub-Request` 调用 `POST /api/v1/desktop/ticket`，取得 60 秒内单次有效的 WebSocket 票据（仅内存保存其 SHA-256）。
