# dyc：独立评论采集 CLI（v1）

用独立 Chromium 资料目录，串行采集视频一级评论和楼中楼，导出 CSV 与高赞摘要。
代码独立实现，未读取或复制无许可证的第三方代码。**目前只通过合成页面验证；
`dyc/site.py` 的真实页面选择器、登录信号、字段和接口路径均未校准。**
“complete”仅表示所见分页走到末尾且回复数量无已知缺口，不代表拿到平台隐藏或折叠的评论。

## 使用

在本目录执行，也可从任何目录调用 `bin/dyc`；相对文件路径始终以调用时的工作目录为准：

```sh
bin/dyc check-env
bin/dyc selftest --out /tmp/dyc-smoke-output
bin/dyc login
bin/dyc fetch 'https://www.douyin.com/video/<id>' 'https://v.douyin.com/<short>/' --urls-file urls.txt --out ~/dyc-output/my-batch --top 30
# 等价的 Python 入口（不经过 systemd 内存 scope 包装）：
.venv/bin/python -m dyc --help
```

`urls.txt` 每行一个链接，空行和 `#` 注释忽略；也支持带 `modal_id` 的链接。
按视频 ID 去重；短链由浏览器导航解析。默认输出 `~/dyc-output/<run_id>/`。
`--help` 在每个子命令可用；`--max-pages` 可降低单视频请求预算，不能提高硬上限。
完成时 stdout **恰好一行 JSON**，包含目录、每个视频状态/数量和退出原因；人类日志在 stderr 和 `run.log`。

输出目录 0700，文件 0600：

- `comments.csv`：带 BOM 的 UTF-8，保留用户 ID、昵称、IP 属地；ID 为字符串，缺失为空，数值零保留。
  包含父子关系、原始回复 ID、图片/贴纸标记、线程状态；文本类单元格前导公式字符加 `'`。
  用表格软件导入时仍应把 ID 列选为“文本”，避免长 ID 被软件自动转为浮点数。
- `summary.md`：逐视频状态、展示/报告/采集数量、未完整线程及缺口、一级和回复 Top N、跨视频榜单。
  不输出昵称、用户 ID、IP 属地字段；回复附根评论正文，文本进行 Markdown/HTML 转义。
- `run.json`：机器摘要；不保存请求模板、请求头、原始 API 响应或签名。
- `run.log`：固定事件/原因码，不打印异常原文、请求 URL 查询串、Cookie 或签名参数。

## 服务器部署与登录

仅把此目录复制到 `~devbot/douyin-comments`，以 devbot 自己的账号运行，不碰现有指数服务：

```sh
cd ~/douyin-comments
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
bin/dyc check-env
bin/dyc selftest
```

标准库 + `playwright==1.64.1`；已用 Python 3.13 验证。服务器需已有 Xvfb 和对应版本的 Chromium。
未设置 `PLAYWRIGHT_BROWSERS_PATH` 且 `/opt/ms-playwright` 存在时自动使用它；不自动下载浏览器。
`bin/dyc` 在用户 systemd manager 可用时使用 `systemd-run --user --scope --quiet -p MemoryMax=1500M`。
不可用时向 stderr 发警告并继续，**此时不强制内存上限**；`DYC_NO_SCOPE=1` 可显式关闭 scope。
`DYC_PYTHON` 可指定解释器，否则优先本目录 `.venv/bin/python`。
启动可用内存必须至少 2 GiB。除非显式传 `--display :N`，即使环境已有 DISPLAY，也另起自己的
`Xvfb -displayfd`，用私有 0700 临时目录内的 0600 Xauthority 文件和随机授权 cookie 保护本地显示，
通过 `-auth` / `XAUTHORITY` 仅交给自己的 Xvfb 和 Chromium，退出或启动失败时删除授权文件，只清理本工具的进程。

资料目录默认 `~/.local/share/dyc/profile`（`--profile-dir` / `DYC_PROFILE_DIR`），必须归当前用户、0700，
拒绝符号链接和第二实例。持有 flock 后才清理确认已失效的 Chromium Singleton 锁；仍存活或来自其他主机的锁拒绝启动。
状态目录默认 `~/.local/state/dyc`（`--state-dir` / `DYC_STATE_DIR`），同样要求 0700。
浏览器登录资料持久保存于自己的 profile，不读取现有指数工具或系统浏览器资料。

`login` 截取二维码区域；等待期间在 stderr 给出私有 PNG 路径，供操作人安全取图扫码。
最长等待 180 秒；成功、超时、SIGINT/SIGTERM 都删除 PNG；结束 JSON 中的路径是已删除文件的位置。
资料目录仍含登录态，应按凭据保护，不提交、不上传。首次真实访问及小号扫码前仍需按项目约定先回报；
本实现和离线验收均未做真实访问或真实扫码。

## 限制、退出与风险

每批最多 1500 次、上海自然日最多 3000 次；计数保存在状态目录的 `usage.json`，只有日期和次数。
页面自发的模板请求、分页请求和重试均计数；其它静态资源不计评论请求预算。每次请求后随机间隔 2–4 秒，
每第 15 次额外 8–15 秒，换视频 20–40 秒；这些是工程约束，不是官方配额。
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
DYC_REQUIRE_BROWSER_TESTS=1 xvfb-run -a /tmp/dycvenv/bin/python -m unittest discover -s tests -p 'test_*.py'
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
