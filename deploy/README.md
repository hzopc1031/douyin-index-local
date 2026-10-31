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

Qt 持久 profile 启动保护：pywebview 6.2.1 为每个窗口创建同名持久 QWebEngineProfile，双窗口会卡死并快速耗尽内存。`webview_profile_patch.py` 仅在 Hub 持久模式、版本精确匹配且 Qt WebEngine 可用时，让控制窗口与官方窗口共享同一个 profile 实例，并设置 `ForcePersistentCookies`，使没有 Expires/Max-Age 的会话 Cookie 也能在重启后恢复；默认 `AllowPersistentCookies` 会丢失这些登录 Cookie。版本、枚举或 setter 检查失败记录警告并回退无痕。`INDEX_HUB_PERSIST_LOGIN=0` 仍直接使用无痕模式。

Hub 两种模式均有启动 watchdog：`INDEX_HUB_WEBVIEW_START_TIMEOUT` 默认 90 秒（10–300），`INDEX_HUB_WEBVIEW_MAX_RSS_MB` 默认 2500 MB（256–3000，Linux 进程及子进程 RSS 总和）。官方窗口加载超时或启动期间超内存即释放 profile 锁，以 75 退出，由 systemd 重启，不在进程内重建 Qt；官方窗口首次 loaded 后 watchdog 停止，不限制交互登录阶段的 RSS。生产观测正常启动约 1.3–1.4 GB（一次为 1.37 GB），交互登录峰值约 2.55 GB，原 MemoryHigh=2500M 会触发节流而非杀进程。单元改为 MemoryHigh=2800M，以避免该登录峰值触发节流；MemoryMax=3G 仍为硬停止上限，MemorySwapMax=0 保持不变。使用 control-group 清理所有子进程。systemd >=254 支持 15–120 秒递增重启间隔；15 分钟内 4 次失败后保持 failed，排查后执行 `sudo systemctl reset-failed douyin-index-desktop.service`，再 `sudo systemctl start douyin-index-desktop.service`。

Hub 持久模式还安装 `webview_shutdown.py` 的 SIGTERM 处理：在 GUI 线程退出 Qt、先销毁页面再销毁共享 profile，随后释放 profile 锁并让解释器正常退出以完成 Cookie 写入。首次信号启动不可延期的 5 秒退出期限；卡住时释放锁并 `os._exit(143)`，不保证强制退出时的 Cookie 刷盘。普通桌面和 `INDEX_HUB_PERSIST_LOGIN=0` 不安装此处理。设置 Cookie 约 2 秒后 SIGTERM 的本地回归已验证会话及持久 Cookie 经生产退出路径后能在重启请求中恢复。

Cookie 恢复还需要 `webview_cookies.py`：本机 Qt 6.12.0 在 GUI 线程调用 `loadAllCookies()` 也不重放磁盘 Cookie，pywebview 原始镜像重启后为空。生产 hook 因此在 Qt 打开 profile 前，通过 SQLite `mode=ro` 只读取两个允许域的 Cookie 来初始化内存镜像，随后用 QNetworkCookie 字段处理实时增删；`login_state` 和 Python 请求均使用此镜像。启动读取兼容已提交的 WAL，不创建副本或导出 Cookie。实测 schema 24 使用明文 value；加密值、损坏或无法读取时返回 `unknown`，不使用不完整会话。Qt 6.12.0 的相同会话 Cookie 覆盖会产生一对删除信号，已针对同一 GUI 批次合并，真实删除仍生效。详见 [HUB_API.md](../HUB_API.md)。

本地 Qt 回归使用生产 profile hook、两窗口和 127.0.0.1 合成页面，两组各连续启动三次，覆盖普通退出及设置 Cookie 约 2 秒后 SIGTERM。每次均检查 HttpOnly 会话 `sessionid`、HttpOnly 持久 `sid_guard`、普通持久 Cookie 的来源及属性，并让生产 `_client()` 创建的 requests.Session 发往 fixture echo，确认全部发送；重启后 `login_state=cookie_present`，空 profile 为 `no_session_cookie`。另验证替换、删除、相同 Cookie 重设及 localStorage。loopback 放行仅通过显式测试参数启用，生产域名和 HTTPS 限制保持固定。本次未验证真实抖音登录或在线取数。外部 watchdog 限制每个进程树 1500 MB / 45 秒（低于 60 秒要求）；原始双 profile 缺陷的负对照默认不运行，可用 `INDEX_HUB_QT_NEGATIVE_CONTROL=1` 启用，限制 1500 MB / 20 秒。
