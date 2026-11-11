# 指数工作台 v1

网页与 API 运行在腾讯服务器，当前入口 `https://43.142.104.172:8443/`。
网页登录沿用远程桌面的管理账号和密码。远程桌面在
`/desktop/vnc.html?autoconnect=1&resize=scale&path=websockify`，用于官方扫码和人工验证。
网页查询框直接使用访问者电脑的中文输入法。

## 当前接入范围

- 抖音：全国、aweme、关键词搜索指数，1—5 个词、连续 最近两年内的自定义日期。
- 百度指数：独立浏览器取数已接入，真实登录与逐日核对待验收；Google Trends 待接入。
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
| GET `/history` | read | 只读最近三十组采集结果与口径，以及本地已知最新搜索日 |
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
既有 `results/*/result.json` 在网关启动时导入，用户人工报告以独立 `user-confirmation.json` 为依据。
`GET /history` 只读取 SQLite，不扫描文件或导入；运行中新增的桌面结果要等下次网关启动才导入。
Hub 在线任务仍在完成时直接入库，不依赖历史页面刷新。
历史 GET/HEAD 的 Bearer 鉴权也不更新密钥 `last_used`，确保这条请求链路不写数据库；其他接口仍记录密钥使用时间。
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

## 热榜快照（第一期）

热榜与搜索指数分开。数据来自免登录的 `https://aweme.snssdk.com/aweme/v1/hot/search/list/`，不走创作者中心，不使用浏览器 Cookie，也不占用每日 50 次搜索指数额度。热度不是搜索指数，也不是创作者中心综合指数。缺失为 null；未上榜不记为 0 或第 50 名。

调度在网关进程内，默认关闭。只有环境变量 `INDEX_HUB_HOTLIST_ENABLED` 精确为 `1` 时才启动循环，约每 5 秒检查一次是否到期，到期才发请求。`INDEX_HUB_HOTLIST_INTERVAL_SECONDS` 默认 3600，小于 1800 时按 1800 执行，非法值回退 3600。没有更早的尝试记录时，开启后的第一轮会抓取；之后至少间隔上述秒数。每个上海自然日最多 48 次成功抓取。失败不补跑。HTTP 429、403 或响应不是 JSON 时暂停热榜，暂停状态写在 SQLite，重启后仍然有效。须管理员调用 `POST /api/v1/hot/resume`（需要 `X-Hub-Request: 1`）后才会再抓；恢复本身不会立刻补一次。

| 方法 / 路径 | 权限 | 作用 |
|---|---|---|
| GET `/hot/latest` | read | 最近一次完整快照，不触发抓取 |
| GET `/hot/word?sentence_id=` 或 `?word=` | read | 该话题实际上榜记录；两者只能填一个 |
| POST `/hot/resume` | 仅管理登录 | 解除热榜暂停，不消耗搜索指数额度 |

`GET /status` 增加 `hotlist`：`enabled`、`paused`、`interval_seconds`、`daily_success_limit`、`daily_successes`。搜索指数的 `daily_online_attempts` 与此无关。

## 飙升词（第二期）

`GET /hot/rising` 只读取本库 `hot_snapshots` 和已保存的搜索指数，不抓热榜，不把官方 `trending_list` 当作飙升，也不创建搜索指数任务、不增加 `daily`。Bearer `read` 即可。响应含 `thresholds`、`current_fetched_at`、`baseline_fetched_at` 和每个当前上榜话题的各列。缺失为 null。页面「飙升」视图把 null 显示为 —。

当前榜是最新一张完整快照。基线是早于它至少 `min_age_seconds`、至多 `max_age_seconds` 的最近一张；默认 1800 和 21600 秒。没有合格基线时，名次上升、热度变化及其命中为 null，不把缺失名次写成 50。

- 新上榜：主键（有 `sentence_id` 用 `id:` 加该值，否则 `word:` 加精确词）在当前快照之前、回看 `lookback_seconds`（默认 86400）内没有出现。
- 名次上升：`rise = 基线名次 - 当前名次`。默认命中为 `rise >= 10` 且当前名次 `<= 15`。
- 热度变化：两边 `hot_value` 都为正时，`pct = (当前 - 基线) / 基线`。默认命中为 `pct >= 0.5` 且增量 `>= 500000`。0 或缺失不算正数。
- z：只用同一 `sentence_id` 在当前快照之前的非空热度。少于 `z_min_samples`（默认 8）或样本标准差为 0 时 z 为 null。标准差用 n-1。默认 `z >= 2` 记为辅助命中。没有 `sentence_id` 的话题不计算 z。
- 搜索指数环比：按话题原文精确匹配已存搜索指数，取最近一个非空日和它的前一个日历日。前一日缺失、为 null 或不大于 0 时环比为 null，不用更早的日期代替，也不读综合指数。

阈值环境变量：`INDEX_HUB_RISING_MIN_AGE_SECONDS`、`INDEX_HUB_RISING_MAX_AGE_SECONDS`、`INDEX_HUB_RISING_LOOKBACK_SECONDS`、`INDEX_HUB_RISING_RANK_RISE`、`INDEX_HUB_RISING_RANK_MAX`、`INDEX_HUB_RISING_HEAT_PCT`、`INDEX_HUB_RISING_HEAT_DELTA`、`INDEX_HUB_RISING_Z_MIN_SAMPLES`、`INDEX_HUB_RISING_Z_MIN`。非法值回退默认。settings 键 `hot_rising_thresholds` 里字段合法时覆盖对应环境变量。若最小基线年龄大于最大年龄，这两个字段回退默认。接口和页面都返回实际生效的阈值。

部署时不要在未确认前打开开关。开启步骤：只给 gateway 单元增加 `INDEX_HUB_HOTLIST_ENABLED=1`（间隔可选），然后重启 gateway。desktop 单元不用改，热榜不经过 Unix 桥。`hub.html` 由每次请求读盘，页面更新后刷新即可；调度和 API 随 gateway 进程生效。从 `server-snapshot-2026-10-30` 升到本分支的完整步骤见 [docs/UPGRADE.md](docs/UPGRADE.md)。

## 关键词组（第三期）

`keyword_groups` 只保存组名和关键词。三个分数在读取时计算，不写入 `points.composite`，不创建采集任务，不增加每日 50 次额度。搜索指数仍由用户在趋势页或 `POST /queries` 手动提交。

| 方法 / 路径 | 权限 | 作用 |
|---|---|---|
| GET `/groups` | read | 列出关键词组 |
| POST `/groups` | query，或带管理登录 | 创建。管理员 POST 仍要 `X-Hub-Request: 1` |
| PUT `/groups/{id}` | query，或带管理登录 | 更新名称和关键词。不新建组，不提交搜索指数任务，不删除已存搜索指数。管理员 PUT 仍要 `X-Hub-Request: 1` |
| DELETE `/groups/{id}` | query，或带管理登录 | 删除组，不删除已存搜索指数 |
| GET `/groups/{id}/score` | read | 计算三个分数 |

更新沿用创建时的 `name`、`keywords` 校验，组 id 与创建时间不变。保存后三个分数按新的关键词列表重算；缺数据仍为 null，不补 0。

## 热点宝·话题采集

话题榜不自动轮询。`INDEX_HUB_TOPIC_ENABLED` 仍默认关闭，即使设为 `1` 也不会启动定时轮次。工作台在选中具体垂类时，或在该垂类上切换总榜/飙升时，调用一次 `POST /api/v1/topics/collect-once`。选「全部」只读已保存快照，这个接口也拒绝 `category=全部`。同一榜（总榜或飙升、垂类、近 1 天）在 10 分钟内，或已有请求在进行时，不再打浏览器。请求仍由 desktop 里的热点宝页面自己 `fetch`，不签名、不读取 Cookie。两次请求至少间隔 15 秒，上海日最多 30 次。采集请求串行排队，等待锁和间隔合计最多 30 秒；超时返回 429 `interval`，达到每日上限返回 429 `daily_limit`。空榜也在 10 分钟内复用。垂类保存父级和叶子。请求体与热点宝话题页一致，带 `tag_version: "v2"`。「全部」仍是 `tags: []`。父级（例如科技）发送 `[{"value": 父级数字, "children": [{"value": 每个子级数字}]}]`。叶子发送 `[{"value": 父级数字, "children": [{"value": 叶子数字}]}]`。名称对不上已保存分类时直接返回 400 `invalid_parameters`，不请求浏览器。业务码 5（参数不合法）记为 `invalid_params`，只让这一次失败，不暂停。未登录、验证码、限频、拒绝访问或无法识别的响应仍会暂停，不自动重试。`POST /api/v1/topics/resume` 只清除暂停。

手动打一次（不开启调度），在 desktop 服务已经运行、浏览器进程持有 Unix socket 时，用服务账号执行：

```bash
cd /srv/work-ecs-projects/douyin-index-local
sudo -u douyin-svc env INDEX_HUB_STATE_DIR=/var/lib/douyin-index INDEX_HUB_LOG_DIR=/var/log/douyin-index \
  .venv/bin/python topic_board.py --probe --rank total --category 全部
```

先拉垂类用 `--probe --tags`。输出含 `status`、`item_count`、`fields` 和 `summary`。`summary` 只有 HTTP 状态、最终 URL 的主机和路径、content-type、顶层 code/message，以及 JSON 的键和类型；不包含正文字符串、普通数字、Cookie 或 token。同一份摘要追加到 `topic.log`。`request.through_page_fetch` 表示请求由热点宝页面的 `fetch` 发出；`fetch_hooked` 表示该函数已被页面替换；`extra_query_keys` 只列出实际 URL 比我们多出来的参数名（例如 `a_bogus`），不含参数值。打开页面时先用首页，若 `fetch` 仍是原生函数，再依次试 `/ranking`、`/topic`、`/analysis`，仍不自行签名。`status` 为 `ok`、`not_logged_in`、`verification_required`、`rate_limited`、`access_denied`、`schema_mismatch`、`invalid_params`、`invalid_parameters`、`paused`、`interval`、`daily_limit` 或 `browser_unavailable`。父级示例：`--probe --rank total --category 科技`。叶子用已保存的子级名称。需要先 `--probe --tags` 重拉垂类，父级和叶子都会出现在列表里。管理员也可以 `POST /api/v1/topics/collect-once`，JSON 为 `{"rank":"total","category":"全部"}`。

搜索指数分：同一日历日每个词都有非空搜索指数才计算。当前日取最新的齐全日，基线取最早的齐全日并记为 100。基线合计为 0 时不归一化，搜索分为 null，原始合计仍返回。没有齐全日时搜索分为 null，并列出参考日缺了哪些词。

热榜分：没有完整快照时为 null，这时不能把词标成未上榜。有快照时，精确相等的词用 `hot_value / 榜内最大热度`，全组都有正热度才取平均再乘 100。任一未精确上榜或热度缺失，热榜分为 null；未上榜词单独列出。子串关系只出现在 `substring_hits`，不进入分数。

融合分：搜索分和热榜分都有值时为 `search_weight × 搜索分 + hot_weight × 热榜分`，否则 null。默认权重 0.7 和 0.3。环境变量 `INDEX_HUB_GROUP_SEARCH_WEIGHT`、`INDEX_HUB_GROUP_HOT_WEIGHT` 取 0 到 1 的有限数；settings 键 `group_score_weights` 的合法字段再覆盖。页面显示实际权重。文案固定为工作台热度分，不是搜索次数，也不是抖音综合指数。

关键词组的新建、编辑、删除和查看分数在趋势总览里完成，侧栏不再单开一页。

## 热点宝·话题（待联调）

`GET /topics` 只读 `topic_snapshots`。默认 `window=1d`（近1天）、`rank=total`（总榜，另一值为 `rising` 飙升）、`category=全部`。垂类必须是响应里的 `categories` 之一。没有快照时 `items` 为空。调度开关 `INDEX_HUB_TOPIC_ENABLED` 仍只表示定时轮次是否允许，`enabled` 照实返回；它不决定按需页面的文案。尚未采集时 `status=available`、`status_label=尚未采集`。已经成功返回空榜时 `status_label=该垂类暂无话题`。已暂停仍是 `paused`。调度开着且从未采集时仍是 `not_logged_in`（未登录）。这个开关不会发起抓取。真实采集要等服务器 Qt 浏览器登录后联调，当前不调用浏览器、不自动重试，也不伪造话题。已存快照里的热度缺失仍是 null，不补 0。话题、热搜榜和搜索指数彼此分开。

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

Hub 部署默认短信登录；直接运行普通桌面仍默认 desktop。两服务均设置 `INDEX_HUB_LOGIN_MODE=qr` 后，管理员可使用：

| 接口 | 用途 |
| --- | --- |
| POST `/api/v1/login/qr/start` | 启动；同一 Hub 每 10 秒最多一次，超限 `429 qr_rate_limited` |
| GET `/api/v1/login/qr` | 返回 login_mode、state、qr_version、expires_in、message、session.has_session / age_seconds |
| GET `/api/v1/login/qr/image` | PNG，`Cache-Control: no-store`；尚无二维码时 `404 qr_not_ready` |
| POST `/api/v1/login/qr/cancel` | 取消本轮扫码 |

全部接口经过网关认证，仅管理员可调用；普通 API Key 返回 403，POST 使用现有 `X-Hub-Request: 1` 同源 CSRF 校验。`qr_busy`、`qr_disabled`、`qr_unsafe_debug`、`qr_no_display` 返回 409；桥接不可用返回 `503 browser_unavailable`。扫码接口不消耗每日在线次数、不更新取数时间，也不受 15 秒取数间隔限制。交接后的会话只在 `app.py` 进程内存中；认证失效时清空，安全验证请使用远程桌面。登录成功后需在采集任务中手动恢复；不自动重试失败任务。`/api/v1/status` 同时返回 `login_mode`。

交接后的会话只在 `app.py` 进程内存中，不写状态文件和日志。扫码 Chromium 默认使用服务账号所有的 0700 持久 profile，重启后保留登录资料；`INDEX_HUB_PERSIST_LOGIN=0` 时恢复 Playwright 临时 profile（PrivateTmp 下，正常关闭删除，强杀残留直到单元停止）。Chromium 沙箱由 Playwright 默认关闭（`--no-sandbox`），代码显式设置 `chromium_sandbox=False`；开启需另做 AppArmor/userns 配置。

云端 Hub 默认持久化浏览器登录资料到服务账号所有的 0700 目录：`INDEX_HUB_PROFILE_DIR`，默认 `INDEX_HUB_STATE_DIR/browser-profiles`。Cookie 以近似明文形式保存在该目录（无系统钥匙串），任何能以 douyin-svc 或 root 身份读盘的人都能拿到登录态；若目录不归 douyin-svc 所有或是符号链接，app.py 会拒绝启动。禁止提交、上传或打印这些资料。`INDEX_HUB_PERSIST_LOGIN=0` 恢复无痕模式；普通桌面启动仍使用无痕模式。远程桌面先由管理员携带 `X-Hub-Request` 调用 `POST /api/v1/desktop/ticket`，取得 60 秒内单次有效的 WebSocket 票据（仅内存保存其 SHA-256）。


## 手机号登录（实验，选择器待校准）

Hub 启动脚本和 systemd 模板默认 `INDEX_HUB_LOGIN_MODE=sms`；显式 `desktop` / `qr` 保留原有入口。端口仍为 8443。短信只操作 app 内现有官方窗口，不另开 profile。全部接口仅接受管理员 Basic 认证；API Key 返回 403，缺少认证返回 401，远程桌面票据不能访问本组接口。POST 必须有 `X-Hub-Request: 1`，提供 Origin 时必须与 HTTPS 网关同源。JSON 不允许额外字段、重复字段或非有限数值，所有响应 `Cache-Control: no-store`。

| 方法 / 路径（前缀 `/api/v1/login/sms`） | 请求 | 响应 |
| --- | --- | --- |
| GET 空路径 | 无 | 状态快照及 browser；进行中为 login_in_progress，结束后读取浏览器状态 |
| POST `/start` | `{"phone":"13800138000"}`（合成示例） | 立即返回，后台开始登录 |
| POST `/code` | `{"code":"246810"}`（合成示例） | 立即返回，后台通过按键输入 |
| POST `/resend`、`/cancel` | `{}` | 重发 / 取消后的快照 |
| GET `/frame?v=N` | 唯一正整数版本号 | PNG；图片版本不一致返回 409 |
| POST `/pointer` | `{"frame":N,"kind":"tap或drag","points":[[u,v,t],…]}` | 人工回放后快照，最多等待 12 秒 |
| POST `/text` | `{"text":"A1"}`（合成示例） | 向截图中当前焦点输入，最多 8 位字母或数字 |

快照只允许 state、phone（如 `138****8000`）、resend_in、attempts_left、message、frame_id、frame_width、frame_height、challenge_epoch、verification_mode、qr_available。验证码从不回显；裁剪原点、缩放元数据、DOM 题目身份摘要和 nonce 不离开 app。`verification_mode` 为 `auto`（自动验证中）、`manual`（人工截图）或 `desktop`（需远程桌面），非验证状态为 null。

可选识别依赖见 `requirements-solver.txt`：本地 MIT 库 captcha-recognizer 1.0.2 及随包 ONNX 模型，只处理普通缺口滑块，不联网、不下载模型、不调用第三方解题服务。每个完整登录轮次最多 **3 次自动提交**，verify_send / verify_login 共用计数；没有依赖/模型、低置信度、异常或未确认通过时转人工截图，人工拖动仍失败则转远程桌面。旋转题直接转远程桌面。人工和自动轨迹共用 Qt 回放器；页面独立确认倒计时或登录成功，模型输出本身不算通过。

`frame_id` 随图片变化增加；`challenge_epoch` 只在题目或布局/缩放变化时增加。GET `/api/v1/login/sms/qr?v=<frame_id>` 仅管理员可读取当前人工验证帧中检测到的二维码 PNG；`qr_available` 为 false 时不可读取。二维码仅在内存中处理。GET 图片必须匹配当前 frame_id；人工输入接受同 epoch 内较旧的 frame_id，动画本身不会触发 stale_frame。前端在手势期间固定当前图片；换题后取消手势。轨迹使用 0–1 相对坐标和毫秒原始节奏，采样至少 16ms、最多 200 点、最长 10 秒。Qt 主线程执行截图/输入，派发超时 5 秒；手机号与验证码只通过按键输入。截图编码后最多 300KB。

400：invalid_payload / invalid_phone / invalid_code / invalid_text / invalid_frame / invalid_pointer / out_of_bounds。409：login_busy / busy_fetching / sms_disabled / login_not_active / code_consumed / stale_frame / not_in_verification / verification_busy / remote_desktop_required / frame_not_ready。429：resend_too_early / sms_rate_limited（本服务保护），以及原网关认证和请求限频。503：桥接不可用、回放超时或截图不可用。不设手机号白名单。

每轮最多 10 分钟；code_sent 无提交 5 分钟超时。start 间隔至少 10 秒；发码等待至少 60 秒倒计时，每小时最多 3 次、每上海自然日最多 6 次，只记次数不记号码。pointer 每秒最多 3 次，code/text 各每轮最多 5 次；每次发码最多提交 3 次，nonce 和 code_consumed 拒绝重复/旧代提交。管理员短信 status/frame 读取共用独立 180 次/分钟预算，其他请求仍为原 120 次/分钟，避免每秒状态/截图轮询占满取消和提交额度。

开始前写 `paused={"code":"login_in_progress"}`，worker 在网页关闭后也会检查登录结果；结束转为 `manual_resume_required`，保持暂停直到管理员恢复，不重试失败任务。整个登录持有取数锁，与扫码互斥，成功清空旧 QR holder。桥接返回超时属于不确定结果，保持暂停直至状态明确。

手机号、验证码和截图不进日志、SQLite、结果或页面 storage；仅存在内存中，表单提交立即清空。日志兜底遮盖手机号及 phone/mobile/sms/otp/verify_code 等赋值，SMS 异常不格式化原文。Python 字符串不能保证清零；官方页面仍可能自行把手机号写进已授权的持久 profile。短信 DOM 规则集中于 `SMS_SELECTORS`。登录/二维码选择器已按 2026-10-31 公开页面 DOM 校准（未输入手机号、未发短信）：点击页头「登录」，可能跳转 `/jingxuan`，弹窗不在 iframe；验证码登录默认激活，保留 +86；发码是 span、提交是 div，等待控件启用后点击；协议文字仅作提示，没有勾选框。稳定 ID 优先，name/placeholder/aria/text 和 `login-full-panel-` 前缀作为回退。验证检测保留原规则并覆盖可见 verifycenter/verify iframe，忽略隐藏辅助 iframe。滑块尚未见到真实页面，几何/题目身份规则仍待验证，识别器逻辑未改。本次未作真实登录或取数验证。

离线验收使用 `test_sms_browser.py` 的 127.0.0.1 假页面和真实 Qt，包含完整成功流程后的日志/临时文件/SQLite/API 响应泄露扫描。命令与 RSS 测量见 [README.md](README.md)；这些结果不等同真实短信或官方验证通过率。

### 已存扩展日期段（可选网关升级）

`GET /api/v1/insights/ranges?keyword=酵素&kind=audience` 接受一个 `keyword` 和
`kind=audience|related`，需要 `read` 权限。仅扫描本地已存结果和任务，不调用浏览器或平台，
不创建任务、不计在线尝试，不受 `/jobs` 最近 30 条限制。

响应包含 `ranges`（去重的 `start_date`、`end_date`、`status=available|no_data`）、
`attempts`（每段最新任务的起止日期、`status`、`error_code`、`updated_at`）和
`live_request=false`。同段较新的空响应不会掩盖较早已存的有效数据。
不返回画像内容、任务来源、原始错误消息或凭据；无历史返回两个空数组。

该接口改动需重启 gateway 后才生效；不需要重启 desktop。仅替换 `hub.html` 时，
旧网关的 404 会让网页回退到最近 30 条任务、本会话和当前范围已确认的数据，
并明确注明可能遗漏更早日期段。其他读取错误显示列表读取失败，不能当成没有历史。

`GET /insights` 的历史读取不受滚动的近两年下界限制，仍校验日期格式、过去日期、
起止顺序和最多 731 天；在线 POST 仍执行原有近两年限制。网页同时保留本会话未提交/
未确认的任务状态，引导用户沿用原批次继续提交，不把它误标为需要新建查询。

### 完整日期跳过策略与初始日期

`skipped_trend_no_data` 只匹配扩展任务实际 `platform`、关键词、`start_date`、`end_date`，
在最近 24 小时内取该完整范围最新成功/部分/无数据趋势任务；同段已有非空历史值时不跳过。
关联词经现有截止日规则截短后，若没有完全一致的趋势日期段，则不跳过。
任务 `error.source_trend` 包含 `job_id`、`platform`、`keyword`、`start_date`、`end_date`，
说明这是中枢跳过，并非关联/人群平台返回无数据。没有数据库迁移，不更改旧任务记录。

`GET /history` 另外返回 `latest_available_date`（YYYY-MM-DD 或 null）及
`latest_available_date_source`。抖音默认截止日按官方搜索指数 T+1（上海今天减一天），
不把本地已存最新搜索日当作默认；否则完整查询会一直沿用过期的本地截止日。
`latest_available_date_source` 为 `official_cadence`（按更新节奏推算）、
`official_latest`（最近一次覆盖该截止日的趋势采集确认的实际最新日，约 12 小时）或
`stored_search_index`（该次采集失败或无数据时回退到本地已存最新搜索日）。
历史接口本身只读，不向官方探测最新日。百度路径不变。页面首次初始化截至该日最近 30 天；
未知时留空，未确认批次保留原日期，刷新不覆盖用户选择。
Store.latest_available_date() 仍只反映本地已存全国抖音搜索日值。

### 关联词独立截止日（周日对齐）

`GET /history` 的 `related_latest_available_date` 取 today−lag（默认 3 天）当日或之前
最近的周日，不再与本地搜索指数最新日取较早者。本地趋势可能因默认截止日未推进而停在更早
的日期，不能作为官方关联词是否已出数的依据。没有预先向官方探测最新日。
若最近一次在线请求对该周日返回业务码 500，采集会自动再试上一周日；成功后把该上限写入
`related_sunday_cap`（约 12 小时），历史接口在此期间返回回退周日，避免连续两次消耗配额。
同一周日稍后请求成功则清除该上限。

线上失败与成功对照表明，关联词接口对非周日截止日返回业务码 500；此前“趋势减两天”
的回退只是在趋势刚好停在周二时碰巧落到周日。提交时把截止日对齐到可查范围内最近的周日，
起始日通常保留；若起始日晚于该周日，则保持原区间长度整体前移。原截止日写入
`requested_end_date`。趋势与人群请求不变。
趋势与人群请求不变。任务 query、结果 metadata、缓存键均使用实际日期，
该回退不往查询键增加 requested_end_date，显式查询同一实际范围可命中已存结果。
网页关联词概要及详情标明实际日期段。只读 GET 保留指定范围，网页先查原范围，
无可用结果时再读取回退范围；不会因新上限而隐藏原范围已有的有效结果。
若回退范围也无结果，复用已存日期段接口查找同一起始日、不晚于回退截止日的最近有效范围，
并按实际元数据标注，避免趋势最新日推进后丢失旧的回退结果。
旧版带 `requested_end_date` 的结果兼容读取，包括同时平移起止日的短区间；不迁移或重写缓存，在线缓存仅匹配实际日期。
扩展任务在管理员可见的请求来源中保存规范化的原始查询，幂等重放先核对原始查询，再沿用已接受任务的实际日期；不同查询返回 409。
网页按已提交的批次/任务日期匹配状态，不因最新趋势日推进而隐藏排队或失败任务。

部署须同时更新源码和 HTML，随后只重启 gateway 一次并刷新页面；无需重启 desktop。
新上限仅由网关传入，旧采集进程收到已规范化的日期，默认 today−3 校验不会再次截短。
没有数据库迁移，不自动重试旧失败任务。具体步骤及回滚见本次交付报告。

## 百度搜索指数

`platform=baidu` 接受 1–5 个唯一词、全国、最近两年内最多 731 天；默认七天。整体/PC/移动日序列在私有表分开存储；缺失不视为零。关联词和画像按单词读取。

管理员使用 `POST /api/v1/login/baidu/open` 打开或聚焦官方登录窗口，在中枢扫码（远程桌面可作备选）；`GET /api/v1/login/baidu/qr` 仅管理员可用，必须携带 `X-Hub-Request: 1`，有 Origin 时须同源，返回禁止缓存的 PNG；二维码未就绪返回 503 `qr_not_ready`；`GET /api/v1/status` 的 `platform_status.baidu.browser` 显示浏览器状态。处理登录、验证或风控后，管理员通过 `POST /api/v1/collection/resume` 指定 `{"platform":"baidu"}` 人工恢复；限频至少冷却一小时，失败任务不自动重试。接口不接收或返回百度 Cookie。配置、凭据边界、部署与七日独立页面验收见 [BAIDU.md](BAIDU.md)。离线测试不代表线上验收。

百度已存趋势和 CSV 导出支持 `device=all|pc|wise|all_devices`，API 默认 `all`；`all_devices` 返回三设备的点，CSV 的 device 列分别为 `all`、`pc`、`wise`。抖音忽略前三种设备参数，拒绝 `all_devices`。网页多词趋势逐词读取已有单词接口，导出逐词合并三设备 CSV；设备切换与导出只读取已存数据，不发起采集。

百度关联需求支持 `kind=related`，按 `end_date` 获取需求图谱单日快照；`GET /api/v1/insights` 与 `/api/v1/insights/ranges` 可用 `platform=baidu` 读取，省略平台默认为抖音。百度画像同样可读取已存结果；关联和画像展示核对状态，画像标注已记录的实际窗口与上游原值。

热点宝登录与垂类刷新接口仅限管理员 Basic 认证，POST 须带 `X-Hub-Request: 1`、同源 Origin（如提供）及空 JSON 对象。`GET /api/v1/topics/login` 返回 `state`（`logged_in`、`need_scan`、`expired`、`browser_unavailable`）和 `qr_available`；`GET /api/v1/topics/login/qr` 返回授权窗口中定位到、按原像素整数倍放大的二维码 PNG，尚未就绪返回 503，均禁止缓存。检测到失效会重新加载授权页，刷新期间不提供旧码。二维码定位依赖 ZXing；解码失败时仍可返回通过定位角检查的二维码。`POST /api/v1/topics/login/refresh` 打开或重新加载授权页。登录状态根据页面判断，实际登录以垂类请求结果为准；不读取或返回 Cookie。`POST /api/v1/topics/categories/refresh` 发起一次 tags 请求，与话题采集共用 15 秒间隔、上海日 30 次上限和异常暂停；暂停后须人工恢复，不自动重试。返回状态、条数和是否实际发出请求。

## 百度热搜榜（草稿）

百度热搜来自 `https://top.baidu.com/api/board?platform=pc&tab=<tab>`，免登录；百度热搜热度不是百度搜索指数，缺失为 null，未上榜不记 0。只保存完整快照，不回填历史。默认榜单依次为 `realtime`、`novel`、`movie`、`teleplay`、`livelihood`、`finance`、`sports`、`new_entertainment`、`internation_news`、`challenge`、`car`、`game`（API 榜单名前加 `baidu_`）；每次采集仍须通过完整性校验。默认每轮请求 12 个 tab，至少保存一个榜单时计为一次每日成功。可用 `INDEX_HUB_BAIDU_HOTLIST_TABS` 指定逗号分隔的已知 tab；默认不调度 drama、documentary、homepage，非法项忽略，homepage 不采集。

独立开关 `INDEX_HUB_BAIDU_HOTLIST_ENABLED=1` 才启动；默认关闭。`INDEX_HUB_BAIDU_HOTLIST_INTERVAL_SECONDS` 默认 3600，最低 1800，非法回退 3600。一个周期逐榜各请求一次，不重试；上海自然日最多 48 个成功周期，至少保存一个榜才计一次。403、429 或非 JSON 只暂停百度调度，管理员用 `POST /api/v1/hot/resume` 和 `{"platform":"baidu"}` 单独恢复；不带范围仍只恢复抖音，恢复不会立即采集。`GET /status` 的 `baidu_hotlist` 独立列出配置和状态，不消耗抖音热榜或搜索指数额度。

`GET /hot/latest?board=baidu_realtime`、`GET /hot/word?board=baidu_realtime&word=...` 和 `GET /hot/rising?board=baidu_realtime` 只读取对应榜单；不提供 board 时默认 `word_list`（抖音）。百度没有 sentence_id，飙升阈值沿用第一期设置，可能需要按百度热度校准，百度话题的抖音搜索指数环比不显示。关键词组融合只使用抖音热搜热度。描述、链接、图片、变化和标签等额外字段暂不保存。

### 百度资讯指数

`INDEX_HUB_BAIDU_FEED_ENABLED=1` 才开启资讯指数采集，默认关闭；每个搜索日期分片额外请求 `/api/FeedSearchApi/getFeedIndex` 和对应的 `/Interface/api/ptbk`，共预占 4 HTTP/分片，关闭时为 2，共用百度 120/400 工程额度。结构异常使整个任务失败并暂停；官方空序列保留缺失，不补 0。完整结果尚未用真实账号验证。

词条身份兼容 `key` 字符串及搜索接口同款数组 `[{name, wordType}]` / `[[{name, wordType}]]`，也兼容 `word`/`name` 原有形状，仍须等于查询词且不重复。只读日志已见 `index[].key` 为 `[{name, wordType}]`，其中 `wordType` 为 number，但看不到 `name` 词面值及 `wordType` 是否为 1；官方页面数值仍待核对。

开关打开时，24 小时内的完整缓存若无资讯指数则不命中，将重新采集搜索和资讯指数。

`GET /api/v1/series?platform=baidu&keyword=…&metric=feed_index` 和 `GET /api/v1/export.csv` 的同样参数只读取已存资讯指数；`device` 仅支持 `all`（可省略）。CSV 的 `metric` 为 `feed_index`。关闭开关后已存数据仍可读和导出。该指标不分 PC/移动，与搜索指数口径不同，不等于搜索次数，结果待核对。

启用需 root 写 systemd drop-in 并执行 daemon-reload，重启 gateway 和 desktop（页内脚本在 desktop）。devbot 的免密 sudo 不能写 `/etc/systemd` 或执行 daemon-reload，与热搜开关相同。重启 desktop 可能中断百度浏览器窗口；持久 profile 通常还在，进行中的登录和任务会被打断。

### 百度媒体指数

`INDEX_HUB_BAIDU_NEWS_ENABLED=1` 才开启，默认关闭；关联词和画像不采集。全国、整体、日粒度，每个搜索分片新增媒体请求及对应 ptbk，搜索、资讯、媒体共用 120/400 工程额度，分别预占：仅搜索 2，搜索加资讯或媒体 4，三者均开 6。结构失败整个任务停止，不自动重试；空值保存 missing，不补 0。开启资讯或媒体时，完整缓存分别必须含 `feed_rows` 或 `news_rows`；两者同时开启必须都有，即使全 missing 也可命中。

`GET /api/v1/series?platform=baidu&keyword=…&metric=news_index` 与同参数 `/api/v1/export.csv` 仅读取已存数据；`device` 仅支持 `all` 或省略，CSV metric 为 `news_index`。关闭开关后仍可读、导出。媒体指数是百度新闻收录的新闻标题含关键词的数量，不是搜索次数，不分 PC/移动，与搜索和资讯指数口径不同，结果待核对。

[官方 Helper](http://index.baidu.com/Helper/) 描述统计口径和低搜索指数时无数据；官方不提供开放 API。[qdata](https://github.com/longxiaofei/spider-BaiduIndex/blob/master/qdata/baidu_index/extended_baidu_index.py)、[common](https://github.com/longxiaofei/spider-BaiduIndex/blob/master/qdata/baidu_index/common.py)、[baidux](https://github.com/iamsk/baidux/blob/master/baidux/extended_baidu_index.py) 和 [抓包笔记](https://blog.csdn.net/CY19980216/article/details/107650145) 提供第三方路径和解析：`/api/NewsApi/getNewsIndex` 的 `word`、`startDate`、`endDate`、`area`，响应 `data.uniqid`、`data.index[]` 的 `key`、`startDate`、`endDate`、加密 `data` 及对应 `/Interface/api/ptbk`。真实响应形状、词条实际形态、官方页面数值和自定义地域能否出媒体指数均未用本项目账号核对；第三方称不支持细分地域，官方帮助则同时对三种指数提到自定义地域。本步仅全国。上线更新需重启读取开关的 gateway 和运行页内脚本的 desktop；本次未部署。

### 百度分地区搜索趋势

默认关闭；只有 gateway 和 desktop 的 `INDEX_HUB_BAIDU_REGION_ENABLED=1` 才允许百度查询体增加 `regions`，例如 `{"platform":"baidu","keywords":["示例"],"start_date":"2026-10-01","end_date":"2026-10-07","regions":["北京","海南"]}`。接受 1–5 个受支持省市中文名，按首次出现顺序去重；`baidu_area_codes.py` 的省/市编码来自 longxiaofei/spider-BaiduIndex，海南、吉林按省编码 930、922。非法地区在发送前拒绝；关闭时字段集合及错误提示保持原样。

全国搜索和原有资讯/媒体先采集，每地区每日期分片再用 `/api/SearchApi/index` 的 `area` 编码及对应 `/Interface/api/ptbk` 采集搜索日序列，不使用画像快照 `/api/SearchApi/region`。资讯、媒体、关联词及画像不采集地区趋势。每地区每分片预占额外 2 HTTP，总额为 `(2 + 2*feed + 2*news + 2*地区数) * 分片数`，计入百度 120/400 工程额度。空值为 missing；地区无数据不使全国成功任务失败，但不会缓存不完整结果。地区结果及接口形状尚未用本项目账号及官方页面核对。

`GET /api/v1/series?platform=baidu&keyword=…&region=北京` 或 `/api/v1/export.csv` 同参数只读该地区；省略地区默认全国。关闭开关后已存地区数据仍可读、可导出。启用部署可分别给 gateway 和 desktop 配置 drop-in：

```ini
[Service]
Environment=INDEX_HUB_BAIDU_REGION_ENABLED=1
```

执行 daemon-reload 后重启 gateway（读取开关）和 desktop（读取开关并运行页内脚本）。部署与独立页面验收说明见 [BAIDU.md](BAIDU.md)。
