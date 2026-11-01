# 手机短信登录（方案 B）

实施日期：2026-11-01，北京时间（UTC+8）。本次交付代码和离线验证，**未部署、未启用端口、未修改 Tailscale、未真实发码**。
2026-11-01 12:58 用户批准：评论工具独立手机登录页，通过 Tailscale 私网访问，扫码备用，不动指数及网关。
下列实际启用步骤仍须用户另行点头。

## 隔离和凭据边界

`bin/dyc login-web` 是独立的短期前台进程，只绑定 `127.0.0.1`（默认端口 `8766`）。
复用 dyc 的 `~/.local/share/dyc/profile` 互斥锁；与 `dyc login` / `dyc fetch` 不可同时占用同一 profile。
始终创建带独立 Xauthority 的 Xvfb；此入口拒绝 `--display`，不使用环境 DISPLAY。
没有指数目录 import、指数 API、网关依赖或服务重启动作；拒绝 `/var/lib/douyin-index` 下的路径。

持久 profile 的浏览器上下文停在空白页；短信/扫码页面在同一浏览器的临时上下文内运行。
成功须同时满足 dyc 已登录信号、登录弹窗消失和 session Cookie 存在，才把白名单登录 Cookie 写回 dyc profile。
页面 localStorage、缓存、历史、手机号、验证码不写回。不导出 `storage_state`、HAR、视频或截图文件。
这比把登录表单直接放入持久 profile 更保守；**Cookie 白名单和成功信号尚待真实双端核验，可能出现已在页面登录但无法持久化的情况**。

号码/验证码仅通过手机浏览器的同源 POST、服务器内存和 Playwright 内存管道交给官方页面；无对应 CLI 参数。
输入框为密码显示、提交后清空。所有 HTTP 响应禁止缓存；不输出请求日志、异常原文、URL 查询串或凭据。
截图先隐藏各 frame 的 DOM 文字、输入框和编辑区域，再裁切验证区域；二维码只截二维码。
验证码图片本身保留以供人工操作。截图仅在内存响应中短期使用，不落盘。
成功、取消、故障、超时和 SIGINT/SIGTERM 都关闭临时上下文、浏览器及自己的 Xvfb，释放 profile。
自动识别运行于可终止的独立子进程，最多等待 10 秒；不让卡死识别任务占住会话。

一次性口令由启动命令生成，只写 `/dev/tty`，不进 stdout/stderr 日志；无 TTY 则拒绝启动。
180 秒内可兑换一次，8 次错误尝试即作废。认证后使用 Secure / HttpOnly / SameSite=Strict Cookie，
校验精确 Host、Origin、JSON Content-Type、CSRF token；每项写操作还有一次性 nonce。
登录会话绝对期限为 600 秒，页面轮询不续期；结束状态保留约 10 秒供手机读取后进程退出。
关闭手机页面不会延长会话。口令或页面状态丢失时在终端 Ctrl-C，然后重新启动；不把口令放进网址。

## 发码和验证

- 发码串行，间隔至少 60 秒，滚动一小时最多 3 次，北京时间自然日最多 6 次。
- 发送前扣计数，发送失败或结果不明也计数，不自动重发。`~/.local/state/dyc/sms-attempts.json` 仅保存时间戳，0600、原子写入并加锁；重启保留限频。时钟倒退、文件损坏或权限不符时拒绝发码。
- 此计数只覆盖这个 dyc 状态目录。指数工具的短信计数独立；同账号其它设备和入口的尝试须人工合计，平台可能有更严格的限制。不要通过换状态目录或删计数规避限频。
- 普通拼图缺口必须同时有明确提示、完整几何结构和高置信度识别才自动拖动。一次进程最多 3 次，切换短信/扫码不重置；同一张题图不重复自动尝试。
- 旋转、点选、跨域 iframe、结构不明确或识别失败均转人工。手机按截图拖动/点选；坐标限于图内，轨迹最多 5 秒。截图变更或重复提交拒绝重放，刷新后再试。
- HTTP 401/403/429 或成功信号不可信时停止；不会因此重发短信。验证码提交一次即消耗该轮机会，错误后重新启动或切换方式，由用户决定是否再次请求短信。

## 管理员准备（用户批准后执行）

由服务器管理员安装/确认 Python 3.12、Xvfb、Playwright 对应版本 Chromium；由 devbot 安装到独立 dyc 目录。
不安装到指数虚拟环境，不共用指数 profile。仅复制 `tools/douyin_comments` 的源码与文档，不复制开发机 `.venv`、测试产物、profile 或状态目录。

```sh
# devbot，在单独的 dyc 安装目录
cd ~/douyin-comments
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-solver.txt
bin/dyc check-env
```

`requirements-solver.txt` 固定本地缺口识别依赖，模型随 wheel 安装，不在登录时下载。
未安装则自动转人工；完整离线测试须安装它。`bin/dyc` 默认使用用户 systemd scope，内存上限 1500M。
启用前确认该 scope 可用；不可用时由管理员另行落实资源限制。运行前至少有 2 GiB 可用内存。
保持前台 TTY；不要用 nohup、终端录制、聊天工具代执行或把一次性口令复制进日志。

## Tailscale 管理员配置（单独批准后）

先由 Tailnet 管理员检查现有 Serve 配置和空闲端口，选择专属 HTTPS 端口，例如 `8444`，保留现有所有映射。
将 Tailnet policy 中到该节点该端口的授权收窄到本人身份/设备，检查并移除会额外放行该端口的宽泛授权。
不要公开分享节点或使用 Funnel。确认手机已加入该 Tailnet、MagicDNS 和 HTTPS 证书可用。

```sh
# Tailscale 管理员：先检查，不覆盖其它服务
tailscale serve status
# 下面是独立端口示例；仅在批准且确认 8444 空闲后执行
tailscale serve --bg --https=8444 http://127.0.0.1:8766
tailscale serve status
```

Serve 的 HTTPS 代理仅在 Tailnet 提供入口，转到本机 loopback；命令和单项关闭方式依据
[Tailscale Serve 官方说明](https://tailscale.com/docs/reference/tailscale-cli/serve)。
以 Serve 实际显示的完整 `https://节点.尾网.ts.net:8444` 为准，以下命令中的示例主机名必须替换。
应用要求 Host 与 Origin 原样一致，不依赖 `X-Forwarded-*`；若代理改写 Host 则会拒绝，先核查配置，不放松校验。
Cookie 按主机隔离而非端口隔离；此节点的其他 HTTPS 服务必须同样可信。

## devbot 启动、本人手机操作（单独批准后）

确认该账号的其它短信尝试没有超出人工合计预算，且 dyc 没有正在采集。
devbot 在私有交互终端执行（默认目录就是原 dyc 专用目录）：

```sh
cd ~/douyin-comments
bin/dyc login-web --port 8766 --origin https://节点.尾网.ts.net:8444
```

真实命令使用 Serve 显示的 ASCII 主机名，不填写手机号、验证码或口令参数。
手机号和验证码不发给操作助手、聊天或工单。启动口令只从本人的终端读出，手动填入手机页面。

1. 手机连接 Tailscale，在浏览器打开上面的私网 HTTPS 网址；3 分钟内填入一次性口令。
2. 输入手机号，点击发码；需要时在截图里完成验证，再输入短信验证码并提交。
3. 自动不处理的验证在图上拖动或点选；截图变化时刷新。也可点“切换扫码”，用抖音 App 扫另一屏幕上的二维码。
4. 页面显示成功后进程关闭。**由用户同时核对 dyc 和指数原有登录是否仍有效**，确认无互踢；不复制两边 Cookie，不自动跑评论或指数查询。
5. 不继续时点击结束或在终端 Ctrl-C。端口会随进程退出释放；Serve 配置需管理员单项关闭：

```sh
tailscale serve --bg --https=8444 off
```

不要使用 `tailscale serve reset`，它会清除其它映射。回滚仅关闭本次前台进程和本次 Serve 端口，
恢复先前 dyc 源码；保留限频状态，不动指数、网关和任何其它服务。

## 开发验证与固定参考

```sh
cd tools/douyin_comments
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-solver.txt
DYC_REQUIRE_BROWSER_TESTS=1 .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

测试在内存中提供假登录页、虚拟 API 和合成验证码图片；全部其它导航拒绝。
认证/HTTP 解析用内存 StreamReader 与假 writer，手机 UI 用 Playwright route 代理，无需开启本地监听端口。
测试使用临时 profile/state 和自己创建的 Xvfb，不触及真实账号，不连接真实抖音。

设计参考固定为 `/workspace/dev/login-B/sms_login.py`（SHA-256 `7aad3a2e4761252106add9b114e63a55b9b0fe8e575adf83903f69a3b1d5b665`），
`/workspace/dev/prompts/B-calib-dom.md` 的公开 DOM 校准；无 Qt 驱动复用或运行时跨目录 import。
`dyc/slider_solver.py` 为 login-B 同名文件的固定副本（SHA-256 `4e7a779a31717265b85757530388ff09a5c12430224e8accfda8c318ed1a0470`）。
合成图片 fixture 同步复制并仅改 dyc 本地类型导入。

离线通过不代表真实 DOM、短信接口、风控、Cookie 恢复或双端登录已验证。
共享主机资源、出口 IP 和账号风控仍存在关联。临时上下文减少浏览器主动写盘，但同用户/root、系统交换和外部转储
仍可读取进程内存；Python 字符串不能保证物理擦除。核心转储已在此入口关闭，部署时由管理员确认交换/转储与终端保密条件。

上线前待用户批准：启用新进程和端口、Tailscale 配置、一次真实短信登录及两边登录状态核对、合并上线。
