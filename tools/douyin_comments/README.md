# dyc：独立评论采集 CLI（v1）

## macOS 本地试验入口

在主项目已创建 `.venv` 的情况下，可以双击主目录的 `setup-comments.command` 安装依赖和 Chromium；它需要联网下载浏览器。也可以在主项目目录运行：

```sh
.venv/bin/python -m pip install -r tools/douyin_comments/requirements.txt
.venv/bin/python -m playwright install chromium
cd tools/douyin_comments
../../.venv/bin/python -m dyc fetch-local 'https://www.douyin.com/video/视频ID'
```

工具打开一个临时浏览器窗口，由使用者完成登录，随后在同一进程采集并导出；验证码、登录失效或平台限制会停止。此入口不使用持久浏览器资料目录，关闭窗口后再次使用需重新登录。结果 CSV 含评论者身份字段，默认保存到个人目录 `~/dyc-output/`；每日请求次数记录在 `~/.local/state/dyc/`。请自行保护这些目录。Mac 已实测登录及有限页数的一级评论采集；回复请求时遇到平台安全验证并停止，完整回复及会话清理仍待验证。真实测试使用了临时降低的内存门槛；正式入口默认需要至少 2 GiB 可用内存。Windows 尚不支持此评论入口。

用独立 Chromium 资料目录，串行采集视频一级评论和楼中楼，导出 CSV 与高赞摘要。
代码独立实现，未读取或复制无许可证的第三方代码。公开登录弹窗已于 2026-11-01 只读校准；2026-11-15 的 Mac 测试验证了登录和部分一级评论，回复及不同页面条件仍需验证。
“complete”仅表示所见分页走到末尾且回复数量无已知缺口，不代表拿到平台隐藏或折叠的评论。

新增独立手机登录入口 `bin/dyc login-web`：仅支持扫码登录，遇验证码或访问限制停止，
仅绑定 loopback，通过 Tailscale 私网访问。认证、限频、部署/启用和回滚步骤见 [SMS_LOGIN.md](SMS_LOGIN.md)。
2026-11-02 已授权部署免口令后台入口；扫码登录与评论采集需在实际环境验证。

## 使用

在本目录执行，也可从任何目录调用 `bin/dyc`；相对文件路径始终以调用时的工作目录为准：

```sh
bin/dyc check-env
bin/dyc selftest --out /tmp/dyc-smoke-output
bin/dyc login
# 扫码等待最长 600 秒；默认仍为 180 秒
bin/dyc login --wait-seconds 600
bin/dyc fetch 'https://www.douyin.com/video/<id>' 'https://v.douyin.com/<short>/' --urls-file urls.txt --out ~/dyc-output/my-batch --top 30
# 等价的 Python 入口（不经过 systemd 内存 scope 包装）：
.venv/bin/python -m dyc --help
```

`urls.txt` 每行一个链接，空行和 `#` 注释忽略；也支持带 `modal_id` 的链接及 `iesdouyin.com` / `www.iesdouyin.com` 的 `/share/video/<id>/` 分享页。
按视频 ID 去重；短链由浏览器导航解析。默认输出 `~/dyc-output/<run_id>/`。
CLI 与中枢共用的落地页信任范围现包含 HTTPS 默认端口的 `iesdouyin.com` / `www.iesdouyin.com`：中间页继续等待，视频分享页解析为规范视频链接，不再报 `untrusted_video_redirect`。
`--help` 在每个子命令可用；`--max-pages` 可降低单视频请求预算，不能提高硬上限。
完成时 stdout **恰好一行 JSON**，包含目录、每个视频状态/数量和退出原因；人类日志在 stderr 和 `run.log`。

中枢评论输入框还支持每行一段 App 分享口令，只取第一个 HTTP(S) 链接。视频页、短链和分享页可混合提交；无效行会整批拒绝并返回行号，不回显原文。短链在点击开始、通过登录探测后的 worker 中，通过采集用的同一个 Qt 窗口及原有窗口锁解析，最多等待约 15 秒；解析失败只影响该条，不计评论请求额度。详细接口与原因码见 [HUB_API.md](../../HUB_API.md)。CLI 的分享口令抽链行为未扩展。

输出目录 0700，文件 0600：

- `comments.csv`：带 BOM 的 UTF-8，保留用户 ID、昵称、IP 属地；ID 为字符串，缺失为空，数值零保留。
  包含父子关系、原始回复 ID、图片/贴纸标记、线程状态；文本类单元格前导公式字符加 `'`。
  用表格软件导入时仍应把 ID 列选为“文本”，避免长 ID 被软件自动转为浮点数。
- `summary.md`：逐视频状态、展示/报告/采集数量、未完整线程及缺口、一级和回复 Top N、跨视频榜单。
  不输出昵称、用户 ID、IP 属地字段；回复附根评论正文，文本进行 Markdown/HTML 转义。
- `run.json`：机器摘要；每视频增加 top_requests/top_items/reply_requests/reply_items/retries/empty_pages，请求含失败，条目为有效分页响应 comments 数组长度之和（含重复，一级内嵌回复另计入采集回复数），空页为有效空数组；去重数量仍为 top_count/reply_count。旧结果字段缺失表示未知。不保存请求模板、请求头、原始 API 响应或签名。
- `run.log`：固定事件/原因码，不打印异常原文、请求 URL 查询串、Cookie 或签名参数。

## 服务器部署与登录

仅把此目录复制到 `~devbot/douyin-comments`，以 devbot 自己的账号运行，不碰现有指数服务：

```sh
cd ~/douyin-comments
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
bin/dyc check-env
bin/dyc selftest
```

标准库 + `playwright==1.64.1`；使用 Python 3.12。服务器需已有 Xvfb 和对应版本的 Chromium。
未设置 `PLAYWRIGHT_BROWSERS_PATH` 且 `/opt/ms-playwright` 存在时自动使用它；不自动下载浏览器。
`bin/dyc` 默认不启用内存 scope；设置 `DYC_USE_SCOPE=1` 后在用户 systemd manager 可用时使用 `systemd-run --user --scope --quiet -p MemoryMax=1500M`。
不可用时向 stderr 发警告并继续，**此时不强制内存上限**；`DYC_NO_SCOPE=1` 可显式关闭 scope。
`DYC_PYTHON` 可指定解释器，否则优先本目录 `.venv/bin/python`。
`login` / `fetch` 启动可用内存必须至少 2 GiB；`login-web` 默认关闭此预检，可用 `--startup-limits` 开启。除非显式传 `--display :N`，即使环境已有 DISPLAY，也另起自己的
`Xvfb -displayfd`，用私有 0700 临时目录内的 0600 Xauthority 文件和随机授权 cookie 保护本地显示，
通过 `-auth` / `XAUTHORITY` 仅交给自己的 Xvfb 和 Chromium，退出或启动失败时删除授权文件，只清理本工具的进程。

资料目录默认 `~/.local/share/dyc/profile`（`--profile-dir` / `DYC_PROFILE_DIR`），必须归当前用户、0700，
拒绝符号链接和第二实例。持有 flock 后才清理确认已失效的 Chromium Singleton 锁；仍存活或来自其他主机的锁拒绝启动。
状态目录默认 `~/.local/state/dyc`（`--state-dir` / `DYC_STATE_DIR`），同样要求 0700。
浏览器登录资料持久保存于自己的 profile，不读取现有指数工具或系统浏览器资料。

`login` 点击语义明确的页头“登录”按钮；弹窗已打开时不重复点击。公开页面跳转到 `/jingxuan`，主页面的 `#login-panel-new` 内短信与扫码区域同时可见，无需切换短信标签。二维码为 `#douyin_login_comp_scan_code img[aria-label="二维码"]`，最多等待 60 秒且须图片加载完成才截取（未找到报 `login_qr_missing`，空容器不算二维码）；准备期间持续复查登录态；若持久会话恢复则直接成功，无需二维码。扫码等待期间在 stderr 给出私有 PNG 路径，供操作人安全取图扫码。
每 20 秒重新截图覆盖同一路径，输入框和手机号形态文本遮盖；不自动解决验证挑战。
默认等待 180 秒，`--wait-seconds 600` 可延长至最多 600 秒；成功、超时、SIGINT/SIGTERM 都删除 PNG；结束 JSON 中的路径是已删除文件的位置。
登录期间 document/XHR/fetch 的 HTTP 403/429 立即保护性停止。登录扫描使用 15 秒总期限，
前台与监视器串行扫描，适应首次 SPA 加载；抓取检查仍为 2 秒。扫描超时仍停止、禁止请求，
但返回环境错误 5 / `state_check_timeout`，不会当作已证实风控。扫码等待到期返回 4 / `login_timeout`。
已检测到验证码立即停止，不等其它 frame；验证截图在浏览器关闭前写入 `login-verify.png`，同样遮盖输入框和手机号。
慢扫描只输出耗时和 frame 数，不输出页面内容、URL 或会话。
资料目录仍含登录态，应按凭据保护，不提交、不上传。首次真实访问及扫码需用户授权。
2026-11-01 10:07（北京时间 UTC+8）经授权在腾讯云尝试一次独立登录，二维码生成前因
`state_check_timeout`（退出码 3）保护性停止，未重试或抓取。
2026-11-01 10:42 起（北京时间 UTC+8）完成腾讯云只读 DOM/截图校准；未提交登录、未请求评论。真实扫码与刷新仍需本次上线验证；该超时本身不能证明平台风控。

## 限制、退出与风险

独立 CLI 默认每批最多 25 次、上海自然日最多 150 次；计数保存在状态目录的 `usage.json`，只有日期和次数。
Hub 的 `comments/status.progress` 复用 Budget 与分页计数，提供请求/批额度、一级/楼中楼有效响应条目数（含重复，不含一级内嵌回复）、当前/总视频序号及上海本地 ISO 开始/结束时间；只含计数和时间，结束后保留到下次开始。
Hub 显式使用每批 1500 次、上海自然日 3000 次，按未处理视频平分剩余额度、余数优先给前面的条目；提前结束的额度供后面视频重新分配。共享 runtime 的硬上限为 1500/3000，CLI 默认和启动检查保持原值。
页面自发的模板请求、分页请求和重试均计数；其它静态资源不计评论请求预算。独立 CLI 每次请求后随机间隔 2–4 秒，
每第 15 次额外 8–15 秒，换视频 20–40 秒；这些是工程约束，不是官方配额。
Hub 复用 Pacer，按取页开始计随机 0.6–1.0 秒（包含响应耗时），每 50 次额外 1–2 秒，换视频 1–2 秒。锁释放后让步 0.05 秒，指数仍可在请求间拿锁；验证码、限频及登录失效的自动暂停不变。用户已接受加速的风控风险。
每页仅对超时/网络错误重试一次；XHR 20 秒计时器实际 abort。二次网络失败作为可疑状态停批。
验证码（含 iframe）、非零业务状态、可疑/空响应立即停整批；不会尝试解题、等待消失或跳到下一条视频。
登录失效单独返回 4；异常和中断导出已收到的数据。v1 **不做 SQLite、断点、自动恢复或后台守护**。
页面自己追加的评论请求会被拦下，后续分页完全由串行调度器控制。
一级评论和回复的模板/分页响应若为 HTTP 3xx，Chromium 在跟随重定向前即拦截并以 `comment_redirect` 停批，不重试；短链页面导航仍可解析重定向。
首个有效响应按视频、线程和请求 cursor 保存在内存中，直接用于分页；达到预算或中断时也保留在导出中。
回复完整性取根评论与各回复页已报告总数的最大值，避免较小或缺失的后续值掩盖已知缺口。

| 退出码 | 含义 |
|---|---|
| 0 | 所有视频完整 / 登录成功 / 环境或自检通过 |
| 1 | 参数、文件或其它错误 |
| 2 | 视频/线程 partial、达到运行中上限、SIGINT/SIGTERM |
| 3 | 验证码、风控或可疑响应，整批停止 |
| 4 | 未登录、登录过期、二维码等待超时 |
| 5 | 内存不足、目录权限/锁、浏览器环境、启动时日额度耗尽 |

云出口与另一个账号共用 IP 存在连带风险；批量采集可能触发平台限制乃至封号。
接口和签名机制会变化；真实校准前不能把离线通过当作线上可用。仅采集有权处理的数据，谨慎保管含身份字段的 CSV。

## 离线测试

```sh
.venv/bin/python -m pip install -r requirements.txt -r requirements-solver.txt
DYC_REQUIRE_BROWSER_TESTS=1 .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

在上述命令末尾加 `-v --durations 0` 可记录每项测试耗时（Python 3.13）。慢 seed 测试用事件控制
“请求获准 → discovery 到期 → 等待解码完成 → 下一视频”的顺序，同时核对 CSV、请求预算和未完成请求；
XHR 超时测试用 Playwright 虚拟时钟核对 19,999 ms 时未 abort、20,000 ms 时 abort 及最多一次重试。
这些测试不会真实等待 16/22 秒，也不缩短生产的 20 秒 XHR、22 秒请求/discovery 或限速设置。
真实 renderer 截止和进程信号清理测试仍使用有界计时；浏览器启动、导出及清理耗时会随共享机器负载变化。

浏览器测试实际运行 `headless=False` Chromium；在任何导航前安装 context 级路由，假域名从内存响应，
其它请求一律 abort，WebSocket 也关闭，service worker 禁用。不需要真实账号，不运行 Python HTTP 客户端或本地接收服务。
`selftest` 使用同一完整 pipeline、临时 profile/state 和仅供假页面使用的快速时钟，绝不触及真实 profile。
CLI 不提供快速采集开关。SIGTERM 测试检查约 8 秒内退出、保留采集结果并检查原进程树无存活子进程。

## 中枢小页诊断（离线）

QtCommentTransport 的网页请求重放在视频/根评论/游标及既有签名更新之外，仅将模板中已有 count 改为 20；楼中楼原值大于 20 原样保留，缺少 count 不新增。网页自发的首次请求原样；不能保证实际每页 20 条，非空短页只要 has_more 为真且游标前进就继续。25 次一级请求返回 24×5+1 条即可得到 121 条且楼中楼仍 pending：CollectJob 始终把下一页一级放到队首，一级结束后才采回复。抖音报告总数可能包含回复，不能直接等同一级数量。Qt 禁止重试，空页、重复页、游标不前进/循环会结束该分支，不反复请求。离线证据见 test_comments_hub.RequestStatsTests 和 test_comments_guard.cjs；没有真实响应无法确认那次运行的 count 或重复条目数。其余重放参数、去签名/msToken 刷新保持原样；保留一级优先以覆盖更多讨论主题。

`test_comments_hub.HubTimingTests` 以生产 QtCommentTransport、CollectJob、LockedTransport 和 HubPacer 配合合成页面适配器跑单视频 1000 条一级评论（每页 20 条），假时钟模拟每次响应 0.3 秒，包含 0.1 秒轮询和锁释放后等待：固定种子为 40.858 秒，所有间隔取上限为 49.35 秒，均 50 次请求、≤60 秒。第 50 次的额外休息会在下一请求/换视频前兑现。此测试不含真实页面加载/模板发现、Qt 调用和导出耗时，也不含指数占锁或网络抖动，不能保证线上端到端 ≤60 秒。页面仅换视频或偏离目标时导航；无模板首屏 5 秒后最多重载一次，既有 0.1 秒轮询和 20/28 秒发现截止保留。
