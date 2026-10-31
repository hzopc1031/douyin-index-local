# 指数工作台 v1

网页与 API 运行在腾讯服务器，当前入口 `https://43.142.104.172:8443/`。
网页登录沿用远程桌面的管理账号和密码。远程桌面在
`/desktop/vnc.html?autoconnect=1&resize=scale&path=websockify`，用于官方扫码和人工验证。
网页查询框直接使用访问者电脑的中文输入法。

## 当前接入范围

- 抖音：全国、aweme、关键词搜索指数，1—5 个词、连续 最近两年内的自定义日期。
- 百度指数、Google Trends：只显示待接入状态，没有模拟或替代数据。
- 搜索指数不等于搜索次数；综合指数分列。跨平台数值不能直接横比。
- Google Trends 将来接入时必须保留地区、查询窗口和相对归一化上下文，不能直接覆写成同一绝对日序列。

## 给 WorkBuddy 等调用方使用

1. 登录网页 →「API 与密钥」，创建独立 API Key。历史读取用 `read`；需要提交任务时用 `read + query`。
2. 完整 Key 仅创建时显示，服务端只存 SHA-256。默认 30 天有效，可指定 1—365 天；可随时撤销。
3. Key 放入调用方凭据配置，例如环境变量 `INDEX_HUB_API_KEY`，不要放入技能说明正文、仓库或日志。
4. 当前证书为自签名。核对腾讯控制台中显示的证书 SHA-256 指纹，在网页下载公共证书 `index-hub-ca.pem`，由客户端将其作为 CA 使用。不要 `verify=False`、`curl -k` 或关闭证书校验。
5. 有公开可信域名证书后可直接使用系统 CA；此版本不修改 DNS，也没有申请公开证书。

源码中的 `hub_client.py` 只依赖 requests，未设置自动重试；对 API 的请求不跟随重定向。

```python
import os
from hub_client import IndexHubClient

client = IndexHubClient(
    'https://43.142.104.172:8443',
    os.environ['INDEX_HUB_API_KEY'],
    ca_bundle='index-hub-ca.pem',
)

# 只读已存储数据，不请求抖音。
saved = client.series('酵素', start_date='2026-10-21', end_date='2026-10-27')

# 示例为已经人工核对过的历史日期，实际截止日须以官方页面为准。
job = client.submit_query(['酵素'], end_date='2026-10-27',
                          idempotency_key='workbuddy-enzyme-20261027-v1')
# queued/running 时可间隔 3—5 秒读取这个 ID，不要重新发起新任务。
job = client.job(job['id'])
client.close()
```

幂等键对应「调用密钥 + 请求内容」。同一键重复提交同一内容返回原任务；内容改变返回 409。
在网络断开或提交超时后，沿用同一键核查任务，不要换键重试。

## API 契约

所有接口前缀 `/api/v1`，第三方请求携带 `Authorization: Bearer <API_KEY>`。
不支持浏览器跨域 CORS；网页同源调用，WorkBuddy 等通过其后端或脚本调用。
OpenAPI 3.1 文档：`GET /api/v1/openapi.json`，需要有效 Key 或管理登录。

| 方法 / 路径 | 权限 | 作用 |
|---|---|---|
| GET `/platforms` | read | 平台、可用性、口径 |
| GET `/status` | read | 浏览器、采集暂停、保护限制和数据统计，不请求抖音 |
| POST `/queries` | query | 提交查询，必须附带 `Idempotency-Key` |
| GET `/jobs` | read | 最近三十个任务 |
| GET `/jobs/{job_id}` | read | 任务状态、标准日序列与完整性 |
| GET `/series` | read | 读取数据库趋势；必填 keyword，可选 platform、start_date、end_date |
| GET `/history` | read | 最近三十组采集结果与口径 |
| GET `/export.csv` | read | 同 series 参数，CSV 中缺失留空，关键词防表格公式注入 |
| GET `/tls-certificate.pem` | read | 公共证书，绝不包含私钥 |
| GET/POST `/keys`、DELETE `/keys/{id}` | 仅管理登录 | 创建、列出前缀或撤销密钥 |
| POST `/collection/resume` | 仅管理登录 | 人工处理官方验证后解除采集暂停，保留冷却和日上限 |

管理网页写操作必须有 `X-Hub-Request: 1`，有 Origin 时须严格同源。第三方 API Key 无管理权限，不能进入远程桌面。

请求 JSON 示例：

```json
{"platform":"douyin","keywords":["酵素"],"end_date":"2026-10-27"}
```

可选 `start_date`，不填默认截止日前六天；填写后允许含首尾共 最近两年内的自定义日期。不支持其他字段或传入 Cookie。
排队返回 HTTP 202，缓存命中或重读完成任务返回 HTTP 200，响应顶层为 `{"job": ...}`。

任务状态：`queued`、`running`、`succeeded`、`partial`、`no_data`、`failed`。
服务重启后，`queued` 任务保留队列顺序继续执行；`running` 任务标为 `failed/service_restarted`（`recoverable: true`），须确认后重新提交，不自动重试。
结果在 `job.result`：

- `rows[].keyword/date/search_index/composite_index`：两种指标分列，缺失为 null。
- `metadata.platform/metric/region/timezone/source/fetched_at`：来源、口径、采集时间。
- `metadata.complete/missing_search_dates`：完整性。
- `metadata.verification`：`pending` 或 `user_reported_match`。后者只代表用户报告已核对，不等于程序持有独立页面观察。

错误格式 `{"error":{"code":"...","message":"..."}}`。任务执行失败则写入 `job.error`。
典型 HTTP 状态：400 参数错误、401 认证失败、403 权限不足、409 幂等冲突或采集暂停、422 平台未接入、429 请求/队列超限。
HTTP 接口可用不等于登录有效。`GET /api/v1/status` 的 `browser` 保留原有状态字符串，新增 `login_state`：`qr_session`（内存扫码会话）、`cookie_present`（检测到关键登录 Cookie 且未发现过期）、`expired`（显式过期时间或 sid_guard 内嵌签发时间与有效期已过期）、`rejected`（相同关键 Cookie 集合已经取数认证失败）、`no_session_cookie`（没有关键登录 Cookie）、`unknown`（无法读取 Cookie store）。桥接响应使用相同的 `login_state`，并保留 `state`。

桌面窗口只有通过官方页面检查且 `login_state=cookie_present` 才是 `browser_ready`；已有内存 QR 会话保留 `qr_session` 就绪路径。其他登录状态均为 `login_required`，任务在取数前停止，不消耗在线次数。状态检查只读取本地页面与 Cookie，不联网、不占配额，不返回 Cookie 值。Cookie 存在与过期检查只是本地启发式判断：服务端撤销或风控导致的认证失效只能由真实取数发现；`rejected` 只识别已经失败的完全相同 Cookie 集合。进程仅在内存保存该集合的 SHA-256 指纹（不记录日志、不持久化、不返回），Cookie 集合改变或进程重启后清除；不能将 `cookie_present` 当作认证成功。

Hub 持久模式下，`login_state` 与 `app._client()` 共用修复后的 `get_cookies()`：共享 profile 创建前，在 profile 锁内通过 SQLite `mode=ro` 只读恢复 `douyin.com` / `creator.douyin.com` 的 Cookie，再由按 `(name, domain, path)` 建立的内存镜像接收 Qt 增删信号。保留 HttpOnly、Secure、Path、Expires 和无过期时间的会话 Cookie，快照排除已过期项。数据库只在启动时读一次，不复制、不导出、不打印 Cookie；运行中的替换和删除不会被旧磁盘数据覆盖。Qt 6.12.0 对完全相同会话 Cookie 的覆盖会发出成对删除信号，镜像合并同一 GUI 事件批次内完全相同的一对；单次真实删除正常生效。

本机 pywebview 6.2.1 / PySide6 6.12.0 实测：即使提前连接信号，并在 GUI 线程于创建时、加载后和延迟时调用 `loadAllCookies()`，重启后的 Cookie 仍不发出新增信号，因此使用上述只读恢复。合成 profile 的 Chromium schema 24 使用明文 `value`；当前回退支持此格式，遇到加密值（含 v10/v11）、损坏或不可读数据库会返回 `unknown` 并阻止使用不完整会话，不猜测解密密钥。本地双窗口回归已验证普通退出及 SIGTERM 重启后，生产 Cookie 来源和生产 `_client()` 创建的 requests 会话均包含三种 Cookie，`login_state=cookie_present`；空 profile 为 `no_session_cookie`。测试仅访问 127.0.0.1，未请求真实抖音数据，不能证明服务端登录仍有效。

## 运行保护

- 完整结果缓存 24 小时，同查询优先缓存，部分结果不缓存。
- 在线请求串行，至少间隔 15 秒，每个上海自然日最多 50 次尝试。
- API 请求每个调用身份每分钟最多 120 次，队列最多 20 项。
- 以上均是本服务保护设置，不是抖音公布的官方配额。
- 平台要求重新登录、验证、拒绝访问或限频时暂停；人工处理后在网页恢复。
- 不自动重试上游失败。服务重启将未完成任务标为中断，不自动重放。
- 无无人值守扫码、验证码绕过或自动刷新登录承诺。浏览器重启需要重新扫码。
- 交接后的 Cookie 仅在 `app.py` 进程及其单次请求客户端内存里；扫码临时 profile 的边界见下文。API 返回经过筛选的业务数据。

## 持久化与部署

运行：原有 `douyin-index.service`，同一个 HTTPS 端口 8443，不新增公开管理端口。
`.cloud-state/index-hub.sqlite3` 使用 WAL，保存任务、密钥哈希、平台日值和来源。
既有 `results/*/result.json` 可导入，用户人工报告以独立 `user-confirmation.json` 为依据。
原始结果、SQLite、会话、密码和证书私钥不提交 GitHub。

应用进程设置 `INDEX_HUB_ENABLED=1` 后启动 0600 的 Unix socket，只有本机同账号可使用。
网关传递查询参数给浏览器进程，绝不接收 Cookie；普通桌面启动保留原来的单次请求限制。
跨任务 API 是用户明确要求的新范围，由任务队列控制；初版没有自动定时采集。

安装核心网页依赖 `requirements-hub.txt`；腾讯浏览器另需 `pywebview[pyside6]==6.2.1`、系统图形依赖及项目内 xcb 库。
数据库备份应在停止服务后复制主文件，或使用 SQLite backup API；不要仅复制 WAL 模式下的主库文件。

离线测试：

```sh
.venv/bin/python -m unittest -q
```

测试使用合成数据和模拟浏览器，不会触发真实平台请求。上线验证优先用既有酵素结果检查网页、Bearer Key、幂等与缓存，不为自检追加抖音查询。

2026-10-28 更新：在线尝试日上限经用户要求从 20 调整为 50，保留 15 秒间隔及异常暂停。趋势页分图展示搜索与综合指数，分别使用自身刻度；综合指数尚未独立逐日人工核对。未进行主动限额压力测试。

任务来源：新建任务记录管理端/API 密钥类型、密钥名称与前缀、连接来源 IP 和最多 512 字符 User-Agent，仅管理登录可查看。忽略 X-Forwarded-For 等未信任代理头。管理端认证也可来自脚本，不声称必定是网页。User-Agent 是客户端声明，不是设备证明。不存认证头、完整密钥或 Cookie。幂等重提交保留首次来源；旧任务显示未记录。

## 扩展数据（试验接入）
POST /api/v1/insights/queries：与 queries 相同的 Bearer/query 权限及 Idempotency-Key，增加 kind=related 或 audience，keywords 恰好一个；支持 start_date/end_date。每个模块独立任务、一次在线尝试，复用全局 50 次日限额、15 秒间隔与 24 小时缓存。结果通过 jobs/{id} 获取。
GET /api/v1/insights?keyword=酵素&kind=related&start_date=2026-10-21&end_date=2026-10-27：仅历史读取；status=available 或 not_collected，result 含 data、metadata。关联词包含 content_related、search_related；画像包含 dimensions[].labels[].name/value/tgi。value 是接口原值，尚未核实百分比单位。无内容时不伪造数据。
官方前端确认 get_relation_word/get_portrait 路由及字段结构，真实取数与页面核对尚待登录后验证。未知字段或结构返回错误，不当成空列表成功。
图表已合并为搜索/综合两条曲线，可独立显隐；原始值共用刻度，相对走势按各序列首个有效日=100归一化，首值为零不归一化。悬浮显示原始值。每个页面顶部都有刷新按钮，仅刷新读取，不触发采集。

## 内部网页完整查询
网页“查询全部模块”会调用现有独立接口：1 个指数批次 + 每词 related/audience 各 1 个任务。1—5 个关键词对应 3—11 个任务，共享全局队列、日配额和请求间隔。对外 /queries 行为不变，不自动附带扩展采集。
网页在当前标签页 sessionStorage 保存本次查询参数、幂等标识、已确认任务 ID；不保存凭据。提交中断时停止继续提交，通过“继续提交未确认任务”手动沿用原幂等标识确认；已确认任务不重复提交。已执行失败的任务不会自动重试。该浏览器标签页之外不提供批次恢复记录，服务器保留独立任务。
报告共用关键词和日期：趋势、搜索/内容关联词、分维度画像分别显示。读取失败仅影响对应模块；空结果、缺失和未采集均明确提示。刷新仅 GET 读取，各模块采集时间独立保留。
前端验证：node test_report_ui.cjs；无需抖音会话或在线请求。

2026-10-28 故障修复：关联词及画像请求体需包在 param 对象中，已与官方前端 post 调用核实。云端串行间隔由 60 秒调整为 15 秒试运行，指数任务优先；50 次每日上限及异常暂停保持。平台 HTTP/业务错误码单独展示，空指数任务标记 no_data。

worker 保护状态码：
- `browser_unavailable`：暂停采集，须浏览器就绪后手动恢复。预检不可用时任务保持 `queued`，不占次数；socket 未监听时回队并退还次数；浏览器已接收后返回此码则任务失败且保留次数，因为可能已请求平台。
- `busy_requeue_exhausted`：浏览器长时间被占用，已放回队列多次仍未执行，未自动重试。`busy` / `cooldown` 最多回队 `INDEX_HUB_BUSY_REQUEUE_LIMIT` 次（默认 20，非法值回退默认，0 直接失败）；耗尽后任务 `failed`，本次仍退还次数。任务的 `requeues` 记录回队处理次数（含耗尽的最后一次）。
- `invalid_query` / `invalid_dates` / `invalid_keywords` / `invalid_kind`：参数错误，请检查请求。任务失败、不暂停，未发送上游请求，退还次数。
- 扩展任务平台明确返回空数据时缓存 `no_data` 24 小时；命中任务保持 `no_data`、`cache_hit=true`，不请求浏览器、不扣次数。读取 `/api/v1/insights` 返回 `status=no_data`，结果含 `no_data=true`，不作为成功数据或零值。

## 扫码登录（实验）

默认仍为远程桌面登录。设置 `INDEX_HUB_LOGIN_MODE=qr` 后，管理员可使用：

| 接口 | 用途 |
| --- | --- |
| POST `/api/v1/login/qr/start` | 启动；同一 Hub 每 10 秒最多一次，超限 `429 qr_rate_limited` |
| GET `/api/v1/login/qr` | 返回 login_mode、state、qr_version、expires_in、message、session.has_session / age_seconds |
| GET `/api/v1/login/qr/image` | PNG，`Cache-Control: no-store`；尚无二维码时 `404 qr_not_ready` |
| POST `/api/v1/login/qr/cancel` | 取消本轮扫码 |

全部接口经过网关认证，仅管理员可调用；普通 API Key 返回 403，POST 使用现有 `X-Hub-Request: 1` 同源 CSRF 校验。`qr_busy`、`qr_disabled`、`qr_unsafe_debug`、`qr_no_display` 返回 409；桥接不可用返回 `503 browser_unavailable`。扫码接口不消耗每日在线次数、不更新取数时间，也不受 15 秒取数间隔限制。交接后的会话只在 `app.py` 进程内存中；认证失效时清空，安全验证请使用远程桌面。登录成功后需在采集任务中手动恢复；不自动重试失败任务。`/api/v1/status` 同时返回 `login_mode`。

交接后的会话只在 `app.py` 进程内存中，不写状态文件和日志。扫码 Chromium 默认使用服务账号所有的 0700 持久 profile，重启后保留登录资料；`INDEX_HUB_PERSIST_LOGIN=0` 时恢复 Playwright 临时 profile（PrivateTmp 下，正常关闭删除，强杀残留直到单元停止）。Chromium 沙箱由 Playwright 默认关闭（`--no-sandbox`），代码显式设置 `chromium_sandbox=False`；开启需另做 AppArmor/userns 配置。

云端 Hub 默认持久化浏览器登录资料到服务账号所有的 0700 目录：`INDEX_HUB_PROFILE_DIR`，默认 `INDEX_HUB_STATE_DIR/browser-profiles`。Cookie 以近似明文形式保存在该目录（无系统钥匙串），任何能以 douyin-svc 或 root 身份读盘的人都能拿到登录态；若目录不归 douyin-svc 所有或是符号链接，app.py 会拒绝启动。禁止提交、上传或打印这些资料。`INDEX_HUB_PERSIST_LOGIN=0` 恢复无痕模式；普通桌面启动仍使用无痕模式。远程桌面先由管理员携带 `X-Hub-Request` 调用 `POST /api/v1/desktop/ticket`，取得 60 秒内单次有效的 WebSocket 票据（仅内存保存其 SHA-256）。
