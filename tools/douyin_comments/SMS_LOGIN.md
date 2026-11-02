# 手机短信登录

2026-11-02 用户授权：部署免口令后台入口，用户只需在手机打开私网地址、输入手机号和短信验证码。Python 3.12；仅操作 devbot 的独立 dyc 安装。不重启指数、网关或桌面进程，不消耗指数查询额度，不读取指数目录或密码文件，不删除已有数据。

## 启动与使用

服务器在 `~/douyin-comments` 已有独立环境时：

```sh
systemd-run --user --unit=dyc-sms-login --collect \
  bash -c '. "$HOME/douyin-comments/env.sh"; exec "$HOME/douyin-comments/app/tools/douyin_comments/bin/dyc" login-web --no-token --port 8766 --origin https://tencent-cvm.taila04aef.ts.net:8444'
```

只绑定 `127.0.0.1:8766`。HTTP 服务启动及本机 `curl http://127.0.0.1:8766/` 不启动浏览器、不发短信；手机页面加载后自动建立登录会话并打开独立浏览器。免口令模式在手机访问前持续等待，不需要 TTY，不生成或打印口令。不要重复启动已有进程。

Tailscale 管理员用独立 HTTPS 8444 映射到 `http://127.0.0.1:8766`，保留其它映射。手机连接该 tailnet 后打开完整 HTTPS 地址，直接看到手机号、发送验证码和 6 位短信验证码输入框；刷新页面恢复当前会话。输入只在手机和登录进程内处理，不记入日志。发送或提交后清空输入框。

遇验证码页、HTTP 401/403/429 或异常即停止并关闭登录浏览器，不解题、不自动重发、不切换扫码绕过。成功显示“登录成功，dyc 登录态已保存”，约 10 秒后进程退出；最终 JSON 只有退出码和固定原因。认证后会话最长 10 分钟，刷新不会续期。关闭手机页面不会立即终止会话，可点击结束按钮。

保留可选口令模式：省略 `--no-token` 时口令仅显示在启动终端；`--token-ttl 1800` 指定有效秒数，默认 30 分钟，单次兑换，8 次错误失效。此模式需交互终端。登录会话期限仍为 10 分钟。

启动内存预检和禁用 core dump 默认关闭，`--startup-limits` 可启用。wrapper 默认不使用 systemd scope；`DYC_USE_SCOPE=1` 可选择 1500M scope，`DYC_NO_SCOPE=1` 优先关闭。没有禁录屏或终端保密确认步骤。浏览器始终创建自己的 Xvfb，不使用指数桌面显示。

## 依赖、验证与部署

```sh
cd ~/douyin-comments/wheels
sha256sum -c SHA256SUMS
. ~/douyin-comments/env.sh
cd ~/douyin-comments/app/tools/douyin_comments
"$DYC_PYTHON" -m pip install --no-index --find-links "$HOME/douyin-comments/wheels" -r requirements.txt -r requirements-solver.txt
"$DYC_PYTHON" -m pip check
DYC_REQUIRE_BROWSER_TESTS=1 "$DYC_PYTHON" -m unittest discover -s tests -p 'test_*.py'
```

本地同样在 `tools/douyin_comments` 用 Python 3.12 虚拟环境运行最后一条命令。测试只用合成页面、图片及临时 profile/state，所有外部导航拦截。solver 的 4 项测试仍验证随 wheel 安装的模型；上线入口不调用 solver。离线通过不代表真实短信、平台成功信号和 Cookie 持久化已经验证。

部署只从已提交版本 archive `tools/douyin_comments`，校验源码 SHA-256 后更新 devbot 的同名源码目录，保留旧源码备份；不 checkout 服务器共享仓库，不覆盖 profile、state、输出、浏览器缓存或虚拟环境。

## 登录后核对与停止

用户确认手机成功提示；管理员检查 `journalctl --user -u dyc-sms-login --no-pager` 的 `login_succeeded` / `exit_code:0`。两者仅表示本次 dyc 成功信号及保存完成；随后用一条公开视频、`--top 30 --max-pages 3` 验证重新打开 profile 后的评论采集。遇验证码、403/429 或登录失效就停。

指数只在已有工作台界面核对登录状态，不发起指数查询；管理员用 `systemctl show douyin-index-desktop.service douyin-index-gateway.service -p Id -p ActiveState -p MainPID -p NRestarts` 比较前后进程状态。进程运行不等于账号已登录，不能据此声称双端登录验证通过。

停止登录服务：

```sh
systemctl --user stop dyc-sms-login.service
```

管理员只撤本次映射：`tailscale serve --bg --https=8444 off`，不使用 reset 或 Funnel。成功退出后无需再停止进程。保留短信计数、登录 profile 和所有结果。

## 保留的行为

复用 dyc profile 互斥锁，不能与 login/fetch 同时使用。手机号和验证码只经同源 POST 与 Playwright 内存管道提交，关闭 HTTP 请求日志、异常原文及 Playwright 调试日志。登录成功必须同时具备页面账号信号、弹窗消失与会话 Cookie，才保存白名单 Cookie 到 dyc profile；不导出浏览器状态。Cookie 持久化与真实页面成功信号仍待上线验证。

原有发码节流保持：间隔 60 秒、滚动一小时最多 3 次、北京时间自然日最多 6 次，发送前计数，失败也计入；不删计数、不自动重发。普通 GET 只提供静态页面；写操作沿用精确 Origin、CSRF 和一次性 nonce。免口令访问权限由私网入口决定。

## 本轮离线结果

2026-11-02：Python 3.12.16 完整 dyc 套件 194 项全部通过，0 跳过，151.816 秒；包含免口令手机页面、刷新恢复、可配置口令期限、无 TTY 启动以及遇验证停止。服务器 wheel 全部 SHA-256 校验通过、离线安装与 pip check 通过，solver 4 项通过。真实短信与评论验证未执行。

指定 `codex exec review --base origin/server-snapshot-2026-10-30 -c model_reasoning_effort="high"` 已完成一轮：无 P0/P1；按用户范围保留两个 P2：旧 CLI 扫码 login 未显式持久化 session-only Cookie；短信登录在截止前刚成功时，截止后的状态请求可能将成功改为过期。后者影响截止附近的成功提示/退出码，真实操作如遇到应记录为未确认。评审内 80 项测试通过，完整浏览器测试受评审沙箱 Xvfb 限制，主环境完整 194 项通过。
