# 手机短信登录

2026-11-02 用户授权：部署免口令后台入口，用户只需在手机打开私网地址、输入手机号和短信验证码。Python 3.12；仅操作 devbot 的独立 dyc 安装。不重启指数、网关或桌面进程，不消耗指数查询额度，不读取指数目录或密码文件，不删除已有数据。

## 启动与使用

2026-11-02 调试入口允许 `--origin` 指定任意 HTTPS 域名，包括 `trycloudflare.com` 隧道；
不再要求 `.ts.net` 后缀。仍须传精确 origin（不含路径、查询串或片段），显式端口仍限 1024–65535；
Host、Origin 和 CSRF 校验保持不变。隧道示例：
`--origin https://discretion-spokesman-pose-fair.trycloudflare.com`。

服务器在 `~/douyin-comments` 已有独立环境时：

```sh
cd ~/douyin-comments
. ./env.sh
nohup app/tools/douyin_comments/bin/dyc login-web --no-token --port 8766 \
  --origin https://tencent-cvm.taila04aef.ts.net:8444 \
  >> login-web.log 2>&1 < /dev/null &
echo $! > login-web.pid
```

只绑定 `127.0.0.1:8766`。HTTP 服务启动及本机 `curl http://127.0.0.1:8766/` 不启动浏览器、不发短信；手机页面加载后自动建立登录会话并打开独立浏览器。此服务器用户未启用 linger，使用 nohup 保持后台运行。免口令模式在手机访问前持续等待，不需要 TTY，不生成或打印口令。不要重复启动已有进程。

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

用户确认手机成功提示；管理员检查 `~/douyin-comments/login-web.log` 的 `login_succeeded` / `exit_code:0`。两者仅表示本次 dyc 成功信号及保存完成；随后用一条公开视频、`--top 30 --max-pages 3` 验证重新打开 profile 后的评论采集。遇验证码、403/429 或登录失效就停。

指数只在已有工作台界面核对登录状态，不发起指数查询；管理员用 `systemctl show douyin-index-desktop.service douyin-index-gateway.service -p Id -p ActiveState -p MainPID -p NRestarts` 比较前后进程状态。进程运行不等于账号已登录，不能据此声称双端登录验证通过。

停止登录服务（核对 PID 对应本次命令，成功自动退出后不必再停）：

```sh
pid=$(cat ~/douyin-comments/login-web.pid)
case "$(ps -p "$pid" -o args=)" in
  *" -m dyc login-web --no-token --port 8766 "*) kill -TERM "$pid" ;;
  *) echo 'login-web 已退出或 PID 已变化，未发送信号' ;;
esac
```

管理员只撤本次映射：`tailscale serve --bg --https=8444 off`，不使用 reset 或 Funnel。成功退出后无需再停止进程。保留短信计数、登录 profile 和所有结果。

## 保留的行为

复用 dyc profile 互斥锁，不能与 login/fetch 同时使用。手机号和验证码只经同源 POST 与 Playwright 内存管道提交，关闭 HTTP 请求日志、异常原文及 Playwright 调试日志。登录成功必须同时具备页面账号信号、弹窗消失与会话 Cookie，才保存白名单 Cookie 到 dyc profile；不导出浏览器状态。Cookie 持久化与真实页面成功信号仍待上线验证。

原有发码节流保持：间隔 60 秒、滚动一小时最多 3 次、北京时间自然日最多 6 次，发送前计数，失败也计入；不删计数、不自动重发。普通 GET 只提供静态页面；写操作沿用精确 Origin、CSRF 和一次性 nonce。免口令访问权限由私网入口决定。

## 本轮离线结果

2026-11-02：Python 3.12.16 完整 dyc 套件 194 项全部通过，0 跳过，151.816 秒；包含免口令手机页面、刷新恢复、可配置口令期限、无 TTY 启动以及遇验证停止。服务器 wheel 全部 SHA-256 校验通过、离线安装与 pip check 通过，solver 4 项通过。真实短信与评论验证未执行。

指定 `codex exec review --base origin/server-snapshot-2026-10-30 -c model_reasoning_effort="high"` 已完成一轮：无 P0/P1；按用户范围保留两个 P2：旧 CLI 扫码 login 未显式持久化 session-only Cookie；短信登录在截止前刚成功时，截止后的状态请求可能将成功改为过期。后者影响截止附近的成功提示/退出码，真实操作如遇到应记录为未确认。评审内 80 项测试通过，完整浏览器测试受评审沙箱 Xvfb 限制，主环境完整 194 项通过。

## 2026-11-02 login_failed 定位修复

准备阶段失败曾被统一隐藏成 `login_failed`。现在会在关闭浏览器前，将固定原因码、步骤、
等待条件、超时预算、实际耗时、脱敏 URL/标题与截图路径写入 stderr 和私有 state 下的
`login-status.json`；失败截图使用唯一 `login-failure-*.png` 文件名，权限 0600。
URL 不保存查询串、片段或未知路径；标题仅保留已知官方落地页标题。截图在栅格化前隐藏所有
文字、输入值、二维码和媒体，保留空白输入框的边框供定位。截图不是原始页面备份。
出现验证或 HTTP 401/403/429 时记录 `BLOCKED`，不继续发码、解题或重试。
手机页面准备失败后显示已停止，具体诊断随状态返回；不再一直显示“正在准备”。

服务器实际观察到官方首页脚本从 `privacy.zijieapi.com` 重定向到 `lf9-sec.bytetos.com`，
原响应保护将其视为危险跳转而停止，另外缺少页面使用的静态 CDN 白名单。新增本次官方页面
观察到的 `douyinstatic.com`、`bytegoofy.com`、`bytetos.com`、`bytescm.com`、
`yhgfb-cn-static.com`；仅静态资源的无输入 HTTPS GET/HEAD 可在批准的资源域名间跳转。
POST、主文档和 API 的重定向限制保留，403/429 仍停止，不放开任意域名。

Chromium dedicated Worker 不支持 `Fetch.enable`。其请求由所属页面 Fetch agent 拦截，
因此不再向 Worker 重复调用该命令；iframe 仍独立安装保护，子目标递归保护保留。
离线真实 Chromium 回归核对 Worker 首个 POST 重定向在跟随前被拦截。
准备流程现在持续检查验证状态，并等待官方手机号输入框可见且可编辑，才返回就绪；
仅出现登录弹窗不代表准备完成。官方账号成功信号、发码和登录持久化仍需另外验证。

本次真实调试严格使用 4 次浏览器打开。前三次确认脚本 CDN 拦截，第四次已到达
`/jingxuan` 并点击页头登录，但随后因 `child_guard_unknown_fetch_enable` 停止，
手机号框数量仍为 0，未发码、未输入手机号。**当前仍 BLOCKED，不是登录就绪。**
最后一次未知子目标类型被旧诊断映射丢失，无法从现有证据确定其真实类型；不能臆测为
Worker 或风控。后续诊断保留 CDP 提供的目标类型和数字错误码，但不记录协议错误文本。
本轮额度已耗尽，未为验证该补充诊断再次打开抖音。部署后的线上单元尚未重启。

## 2026-11-02 第二轮：不支持 Fetch 的子目标

第二轮第 1 次真实打开确认，上一轮的未知子目标实际是 `worklet`，
`Fetch.enable` 返回数字协议错误码 `-32601`（方法不存在），准备流程因此停在
`phone_input_ready`。这是本地 CDP 能力不匹配，本次没有观察到验证码或 HTTP 403/429。

按用户本轮授权，子目标采用能力探测：不支持 `Fetch.enable` 时跳过该子目标的响应保护，
不支持 `Target.setAutoAttach` 时跳过其递归附加，随后恢复目标，让登录组件继续加载。
该规则适用于 worklet、shared/service worker、iframe 及未知类型，只接受明确的
`-32601`；其它协议错误、超时、断开的会话及恢复失败仍停止。主页面 Fetch 保护必需，
dedicated worker 沿用已验证的所属页面保护。不声称所有不支持 Fetch 的子目标流量都被
所属页面覆盖。页面验证检查与 HTTP 401/403/429 停止行为保持。

离线回归覆盖各子目标类型、两个可选命令缺失、非预期错误仍停止，以及真实 Chromium
AudioWorklet 从内存 Blob 加载并运行；所有页面均为合成内容，不需账号或外网。

第二轮验证结果：Python 3.12.16 完整 dyc 离线套件 206 项通过、0 跳过（151.633 秒）。
真实打开使用 3/3 次：第 2 次不再报子目标协议错误，但在 20 秒后报 `phone_input_missing`；
第 3 次仅在调试进程把同一页面等待延长到 60 秒（HTTP 启动预算 90 秒，无刷新），
约 23.4 秒后报 `browser_operation_failed`。最终脱敏截图已出现登录表单轮廓，
但没有获得 HTTP `ready` 和空手机号框可见、可编辑的完整验收证据，故仍为 **BLOCKED**。
调试截图包装与生产准备流程都可能产生这个通用错误，不能据此认定为平台风控，
也不能认定一定是生产准备失败；后续需区分准备完成与截图失败的证据。

只读诊断还观察到本地路由拦截若干资源域名；是否影响登录组件尚未证实，未盲目放行。
未输入手机号、未发码、未提交登录、未采集评论或指数。调试实例与浏览器已退出，
线上 root transient 单元未重启。原 root 重启脚本语法与启动参数仍适用，但须待就绪验收
通过后再由 root 执行。60/90 秒等待仅用于最后一次调试，没有改动生产时间预算。

## 2026-11-02 第三轮：慢加载与静态 SDK

登录入口/手机号框准备等待增至 60 秒，HTTP `/auth` 总预算增至 90 秒；切换登录方式
同样保留 90 秒预算，并受原会话截止约束。手机页面提示可能等待约 90 秒，原 fetch
没有更短的客户端超时，准备期间 busy 阻止轮询和重复操作；不增加自动重试。

对已观察到的 applogcdn、ibytedapm、bytednsdoc、bytetcc、effectcdn、byteeffecttos
资源域，仅允许静态类型的 HTTPS GET/HEAD，以及 fetch/XHR 加载明确的
`.js/.css/.wasm/.json/.bin/.zip` 文件。任意 GET 配置/上报接口、文档和 POST 上报
仍不获授权；主页面 origin、私密输入检测、评论请求拦截与 401/403/429 停止保持。
这只说明可识别的静态依赖可以加载，不代表这些资源都已证实是登录必需项。

调试先记录手机号框 visible/editable/empty 与验证码框 empty 的布尔证据，随后只截一个
可见登录面板，隐藏二维码、媒体和输入值。截图失败单独记录，不再冒充准备失败；
平台访问限制和验证检查仍独立停止。所有真实验收仅看空表单，不输入手机号或发码。
