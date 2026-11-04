# 抖音热点指数方案

本文只描述方案，不改采集代码。范围是在现有抖音搜索指数、Index Atlas 工作台、SQLite 和 Bearer API 之上，增加三块能力：

- (a) 定时抓取抖音热榜，并跟踪话题热度随时间的变化；
- (b) 发现飙升词；
- (c) 给用户自定义的关键词组计算综合热度分。

已有约束保持不变：搜索指数和综合指数分开存储、分开展示；缺失数据不补零；缓存做完整性检查；普通桌面登录会话只放内存；不自动重试已经发往平台的失败请求；遇到验证码或限频立即停止；平台内部接口只做自用、低频抓取。

文中「实测」指 2026-11-04 在本环境对两个公开热榜地址各发起两次 GET。没有跟随重定向，没有携带 Cookie，没有查询串，TLS 校验开启。snssdk 不带 Referer；网页端只带 `Referer: https://www.douyin.com/`。这次没有探测生产网关，也没有重跑仓库测试。

## 已定决策

- 热榜源：以免登录的 `https://aweme.snssdk.com/aweme/v1/hot/search/list/` 为主。`https://www.douyin.com/aweme/v1/web/hot/search/list/` 只作对照，待评审确认后才决定是否长期双源。
- 抓取间隔：每 60 分钟。
- 词条主键：优先 `sentence_id`，缺失时退回精确词。
- 搜索指数每日 50 次额度：目前只手动更新，禁止自动消耗。原第四期「自动划出额度去查飙升词」暂不做。

429 / 403 / 非 JSON 后暂停热榜并必须人工恢复，已作为第一期的保守默认落地。未上榜不记 0、子串不计入热榜分、融合权重默认 0.7/0.3，已在第三期落地。历史回填仍见文末「待评审」。

## 实现状态（第一期）

第一期热榜快照已在 `feature/douyin-hot-index` 实现，调度默认关闭。

- 客户端：`hotlist_client.py`，只请求免登录 snssdk 热榜。TLS 校验、不跟随重定向、不重试、响应大小上限 2 MiB，只保留白名单标量。
- 存储：`hot_snapshots`。主键优先 `sentence_id`，缺失时用精确词。不完整快照不入库。未上榜不写 0、也不写第 50 名。
- 调度：网关进程内独立循环。`INDEX_HUB_HOTLIST_ENABLED=1` 才启动，其他值关闭。间隔默认 3600 秒，低于 1800 秒会抬到 1800 秒。没有更早的尝试记录时，开启后第一轮会抓取。上海日成功上限 48 次，计数键是 `hot_daily`，不占用搜索指数 `daily`。失败不补跑。429、403、非 JSON 写入 `hot_paused`，直到 `POST /api/v1/hot/resume`。
- 读取：`GET /api/v1/hot/latest`、`GET /api/v1/hot/word`，Bearer `read`。OpenAPI 已包含这两条和恢复接口。
- 页面：`hub.html` 的「热榜」页。热度与名次分列，缺失为「—」，并写明不是搜索指数。
- 未做：自动消耗搜索指数额度、网页端对照抓取、历史回填。

第三期关键词组已实现。`keyword_groups` 在 gateway 打开 SQLite 时自动建表。搜索分要求同一日每个词都有非空搜索指数，基线合计为 0 时不归一化。热榜分只在最新完整快照上对精确命中取 `hot_value / 榜最大值` 的平均再乘 100；有一词未上榜或热度缺失则为 null，未上榜单独列出，子串只展示不计入。快照不存在时热榜分为 null，不能写成未上榜。融合分仅在两项都有值时按可配置权重相加，默认 0.7 与 0.3。从 `server-snapshot-2026-10-30` 升级的步骤见 [UPGRADE.md](UPGRADE.md)。

第二期飙升词已实现：`GET /api/v1/hot/rising` 与热榜页「飙升」视图只读 `hot_snapshots`。默认阈值是名次上升至少 10 且当前名次不超过 15、热度涨幅至少 0.5 且增量至少 50 万、z 至少 2（当前快照之前至少 8 个非空热度）。可用环境变量或 settings `hot_rising_thresholds` 覆盖，响应和页面展示生效值。搜索指数环比只在相邻两个日历日都有非空搜索指数时计算。不自动提交搜索指数任务。

## 1. 现状

仓库是单账号抖音搜索指数工具，外加腾讯云上的 Index Atlas 工作台。桌面入口是 `app.py` 与 `ui-batch.html`（pywebview）。云端由 `tencent-desktop.sh` 拉起 Xvfb、x11vnc 和 `app.py`（`PYWEBVIEW_GUI=qt`、`INDEX_HUB_ENABLED=1`）。`hub_bridge.py` 在本机 `0600` Unix socket 上收命令。`tencent-gateway.sh` 启动 `desktop_gateway.py`（aiohttp、TLS、Bearer / Basic）。仓库里没有 Dockerfile、cron 或 systemd timer。`deploy/systemd/` 是拆开的 desktop / gateway 模板，不自动安装。`tencent-enable-service.sh` 仍会写旧的合并单元 `douyin-index.service`，并且会把 `access.json` 打到标准输出。

采集只走创作者中心，不走公开热榜。`client.py` 允许的 POST 只有：

- `https://creator.douyin.com/api/v2/index/get_multi_keyword_hot_trend`
- `get_keyword_valid_date`
- `get_relation_word`
- `get_portrait`

趋势请求体是 `keyword_list`、`start_date`、`end_date`、`app_name=aweme`。搜索指数是 `search_hot_list[].index`，综合指数是同一组里的 `hot_list[].index`。这里的 `hot_list` 是综合指数日序列，不是抖音热搜榜。关联词和画像的业务字段包在 `param` 里（`hub_insights.py` 的 `collect`）。Cookie 从当前官方窗口内存交给当次 `requests.Session`，请求结束执行 `cookies.clear()` 并 `close()`。响应可带 `x-encrypted`（1 或 2），按固定 AES 解密。证书校验开启，`allow_redirects=False`，`HTTPAdapter(max_retries=0)`，无自动重试。

云端限额在 `hub_web.py` 的 `Hub.run_one`：串行、距上次尝试至少 15 秒、上海自然日最多 50 次。登录、验证、拒绝访问、限频会写入 `paused` 并停止。这是本服务保护值，不是官方配额。普通桌面 `app.py` 的 `fetch_batch` 在非 `cloud=True` 时，每次启动只允许一次线上请求。API 不填 `start_date` 时，默认为截止日前 6 天。在线查询最多 731 个含首尾的自然日，且须落在过去两个日历年内。只读历史可以读更早的已存数据。

SQLite 由 `hub_web.py` 打开为 `{INDEX_HUB_STATE_DIR}/index-hub.sqlite3`（`hub_store.py` 的 `Store`）：

| 表 | 作用 |
|---|---|
| `settings` | 日计数、暂停、最近尝试时间等 |
| `keys` | API Key 的 SHA-256、前缀、`read` / `query`、过期 |
| `datasets` | 一次趋势结果全文 |
| `jobs` | 队列；另有后加列 `request_source`、`requeues` |
| `points` | 主键 `(platform, metric, region, keyword, day)`；`value` 为搜索指数，`composite` 为综合指数 |
| `insights` | 关联词 / 画像全文 |

入库平台写成 `douyin`，地区 `全国`。客户端元数据里的平台值是 `aweme`。

主要路由在 `hub_web.py` 的 `mount`：`/api/v1/queries`、`jobs`、`series`、`history`、`export.csv`、`insights`、`insights/ranges`、`insights/queries`、`status`、`platforms`、`keys`、`collection/resume`、`openapi.json`、`login/sms/*`、`login/qr/*`。`desktop_gateway.py` 另有 `POST /api/v1/desktop/ticket`、`/websockify`、`/api/v1/tls-certificate.pem`、`/desktop/`。每个调用身份每分钟 120 次（短信状态读单独 180 次），队列最多 20。OpenAPI 和 `hub.html` 的接口表都比代码少几条。

`hub.html` 侧栏是「趋势总览 / 采集任务 / 平台连接 / API 与密钥」。趋势页用页签区分趋势、关联需求、人群特征、历史。搜索指数和综合指数分色，缺失显示为「—」。百度、Google Trends 在 `PLATFORMS` 里 `available: false`。`baidu_window.py` 只开隔离窗口，没有采集。

部署文档写的工作目录是 `/srv/work-ecs-projects/douyin-index-local`，状态 `/var/lib/douyin-index`，日志 `/var/log/douyin-index`，端口 8443。`HUB_API.md` 记录入口 `https://43.142.104.172:8443/`。

测试是合成数据，不访问抖音。仓库里有 27 个 `test_*.py`，以及 `test_report_ui.cjs`、`test_sms_ui.cjs`、`test_date_ui.cjs`、`test_related_ui.cjs`，另有 `check_hub_ui.py`、`check_audience_ui.py`。README 记载过核心 47 项和后来全量 406 项通过。

`README.md` / `AGENTS.md` 仍指向 `CLOUD_POC.md`、`cloud_probe.py`、`login_transfer.py`、`bootstrap_cloud.sh` 以及对应测试。这些文件现在都不在仓库里。`qr_login.py` 的 `narrow_cookies` 注释写明改编自已不在树里的 `login_transfer` / `cloud_probe`。

## 2. 约束落点

- **搜索指数和综合指数分开。** `client.py` 的 `parse_trend` 分列 `search_index` 与 `composite_index`。`storage.py` 的 CSV 两列。`points.value` 与 `points.composite` 分列。`hub.html` 两条曲线。完整性只要求搜索指数齐全，综合指数可以是 null（`storage.py` 的 `_complete_rows`）。
- **缺失不补零。** `client.py` 的 `_number` 把空、`-`、`--` 收成 `None`。`Store.ingest` 用 JSON 保存 null。`series` 在指定起止日期时会为缺口补上 `value: null` 的占位点，这是响应形状，不是写成 0。导出 CSV 用空字符串。
- **缓存做完整性检查。** 桌面 `ResultStore.lookup` 要求 24 小时内、口径为搜索指数 / 全国 / aweme、`_complete_rows` 为真。Hub 的 `Store.cached` 同样只命中 `metadata.complete`。损坏文件直接跳过。
- **登录态。** 普通桌面 `private_mode=True`，Cookie 只进当次 Session。云端 systemd 模板设 `INDEX_HUB_PERSIST_LOGIN=1`，浏览器资料写到 `0700` 的 `browser-profiles`；`INDEX_HUB_PERSIST_LOGIN=0` 恢复无痕。HTTP API 不接收、不返回 Cookie。Key 只存 SHA-256。`HUB_API.md` 前半仍写「重启后要重新扫码」，后半已经改成可持久化，两段彼此不一致。热榜客户端不应再扩大 Cookie 持久化。
- **不自动重试。** 上游失败、重启时正在跑的任务标 `failed`，不重放。例外只发生在请求未必已经发出时：`busy` / `cooldown` 回队并退还次数，默认最多 20 次（`INDEX_HUB_BUSY_REQUEUE_LIMIT`）；socket 不在时同样回队。参数错误退还次数。限频不回队。
- **验证码或限频即停。** `client.py` 的 `_check_business_status` 把验证、限频、登录失败分成稳定错误码。`PAUSING` 含 `verification_required`、`rate_limited`、`access_denied`、`authentication_required`。恢复只能走 `POST /api/v1/collection/resume`。
- **自用、低频、固定口。** 端点白名单、1–5 词、全国、`aweme`、单浏览器、15 秒 / 50 次。`HUB_API.md` 写明初版没有定时采集。

## 3. 对另一份调研的核对

以下说法与代码一致：桌面 pywebview；云端 Qt 常驻、Unix socket、aiohttp、Bearer、OpenAPI、`hub.html`、systemd 模板；四个创作者接口、Cookie、`x-encrypted`；所述 SQLite 表和主路由；15 秒、50 次、731 天、全国 / 抖音；三份 Cloud PoC 文件确实不在仓库。

需要改口的有六处。

1. `https://www.douyin.com/aweme/v1/web/hot/search/list/` 并非「要游客 Cookie 且返回空」。2026-11-04 的实测里，两个地址都是 HTTP 200、`status_code=0`，`data.word_list` 49 条，词序一致，`position` 为 1–49，词不重复。另有 `trending_list` 5 条，没有 `position`。两次请求之间这 5 条的 `sentence_tag` 分布不一样，不能当成稳定榜单。
2. 词条字段除 `position`、`word`、`hot_value`、`label`、`event_time`、`sentence_id`、`video_count` 外，还有 `sentence_tag`、`word_type`、`group_id`，以及封面、视频列表等嵌套对象。`label` 是整数（本次词榜见到 0、1、3、5、9），没有文字，含义未知。`hot_value` 大约是千万级整数，和搜索指数不是同一把尺子。`event_time` 形如 Unix 秒，接近探测时间；它是话题时间还是榜单时间，仓库里没有说明。
3. 「不自动重试」漏掉了请求发出前的回队。见上一节。
4. 「登录只在内存」只对普通桌面和 `INDEX_HUB_PERSIST_LOGIN=0` 成立。当前云端模板会把浏览器 Cookie 落到磁盘。
5. 「单账号」是部署形态（一个浏览器资料），代码里没有单独的账号配额对象。
6. 接口清单不完整，而且创作者接口里的 `hot_list` 不能当成热搜榜。外部仓库（douyin-hot-hub、DailyHotApi、f2、TrendRadar）这次没有打开，不能把「2021 年起每小时归档、MIT/GPL」写成已核实事实。

## 4. 设计

公开热榜这次不需要创作者登录，因此不要塞进 `DouyinIndexClient._post`，也不要走 `hub_bridge`，更不要占用那 50 次搜索指数额度。另写 `hotlist_client.py`：沿用 TLS、禁止重定向、禁止重试、响应大小上限；只投影白名单标量，丢掉 `aweme_infos`、封面和分享信息。快照完整的条件建议定为：HTTP 200、`status_code=0`、`word_list` 非空、词和名次都唯一。不完整的响应不入库，也不参与飙升和打分。

主源固定为免登录 snssdk 地址。网页端只在评审确认后作为对照探测，不作为入库来源，也不自动跟着重试。

调度放在网关进程里的独立循环，用 `settings` 记 `last_hot_fetch_at` 和单独的日计数，避免和搜索指数共用 `daily`。间隔 60 分钟。上海日成功上限建议 48 次，与每小时一次的节奏对齐；这是建议值，实现前可再确认。仓库里没有 cron。若用 systemd timer，也必须先看暂停标记。失败不补跑。搜索指数额度继续只允许手动提交。

### (a) 热榜快照

新表 `hot_snapshots`：`id`、`fetched_at`、`board`（先只记 `word_list`）、`sentence_id`、`word`、`position`、`hot_value`、`label`、`sentence_tag`、`word_type`、`video_count`、`event_time`、`source`。只存实际出现的行。未上榜不要写成名次 50 或热度 0。`trending_list` 先不入库，等确认它是不是官方飙升榜。

词条主键优先 `sentence_id`。该字段缺失时，用精确词作为退回键，并在记录上标明键的来源。`fetched_at` 与 `event_time` 分开存，不互相填充。

只读 API：`GET /api/v1/hot/latest`、`GET /api/v1/hot/word?sentence_id=`（无 `sentence_id` 时用精确 `word`）。Bearer `read`。`hub.html` 增加「热榜」页，热度与名次分列，缺失为「—」，并写明这不是搜索指数，也不是创作者中心综合指数。

### (b) 飙升词

用本库快照计算，不把官方 `trending_list` 直接叫飙升。比较对象是「至少早 30 分钟、至多早 6 小时」的最近一张完整快照。没有这张快照时，依赖基线的指标为 null。

- 新上榜：主键（优先 `sentence_id`，否则精确词）在过去 24 小时的完整快照里都没出现。
- 名次上升：两张都有名次时，`rise = prev_position - curr_position`。上一名次缺失时，不把名次当成 50 再算跳升。
- 热度变化：两边都有正的 `hot_value` 时，`pct = (curr - prev) / prev`。任一侧缺失则该指标为 null，不补 0。
- 同一主键至少 8 张历史快照且标准差不是 0 时，才算 z-score；样本不够则 z 为 null。
- 搜索指数环比只在两天都有非空搜索指数时计算。缺一天就是缺，不补 0。禁止自动把热榜词送进 50 次队列。

具体阈值见「待评审」。在阈值确认前，可以先把原始 `rise`、`pct`、z 算出来展示，不打「飙升」标签。

### (c) 关键词组综合热度分

新表 `keyword_groups(id, name, keywords_json, created)`。不要写入 `points.composite`。那个字段已经是创作者中心综合指数。

三个数分开显示：

- 搜索指数分：组内每个词在同一个日历日都有非空搜索指数才计算；否则整组搜索分为 null，并列出缺哪一天、哪个词。有基线时用组内自身中位数，或沿用工作台「首个有效日 = 100」。基线为 0 则不做归一化。这一分只来自用户手动提交的搜索指数任务。
- 热榜分：最近一张完整快照存在时，按主键或精确词命中的条目用 `hot_value / 该榜最大值`。快照失败或缺失时，热榜分为 null，不能把「没抓到」说成「没上榜」。热榜词多是整句，和用户短词不会自然相等。
- 融合分：仅当搜索指数分和热榜分都有值时才计算。任一侧缺失则融合分为 null，子分数仍可单独存在。文案固定为工作台热度分，不是搜索次数，也不是抖音综合指数。

未上榜是否贡献 0、子串是否算命中、融合权重，见「待评审」。确认前，融合分先不对外标成单一结论，只并列展示已有的子分数。

API：`POST/GET /api/v1/groups`、`PUT /api/v1/groups/{id}`、`GET /api/v1/groups/{id}/score`。更新只改名称和关键词，不提交搜索指数任务。查询搜索指数仍走现有 `POST /api/v1/queries`，由用户点击，1–5 词，占用原配额。热榜抓取不增加 `daily` 计数。

## 5. 四个阶段

1. **热榜快照。** 热榜客户端、完整快照、每 60 分钟调度、两个只读接口、热榜页、合成夹具测试。主源只用 snssdk。不碰搜索指数额度，不走浏览器桥。
2. **飙升。** 只读计算新上榜、名次变化和热度变化。阈值未评审前只展示原始指标。仍不自动查搜索指数。
3. **关键词组。** 用户手工触发搜索指数。搜索分、热榜分、融合分按「待评审」确认后的规则展示；确认前只并列子分数。
4. **自动额度与历史回填。** 自动消耗搜索指数额度暂不做。历史回填是否做，见「待评审」；未确认前不引入外部归档。

## 6. 测试计划

用合成 JSON，不把 2026-11-04 的线上响应提交进仓库。覆盖：

- 拒绝重定向和非白名单字段；嵌套视频、封面不入库。
- 不完整快照不入库，也不参与飙升和打分。
- null 保持 null。未上榜不记成热度 0 或第 50 名（未上榜如何进入热榜分，等评审）。
- 有 `sentence_id` 时用它做主键；缺失时退回精确词，并记下键来源。
- 热榜抓取不增加搜索指数 `daily` 计数，也不创建 `queries` 任务。
- 429、403 或非 JSON 后不再自动补跑。是否必须人工恢复，按评审结论写断言。
- 搜索指数或热榜快照缺失时，融合分为 null。
- Bearer 只读权限可以读热榜，不能借热榜接口提交搜索指数任务。

前端沿用 `check_hub_ui.py` 的拦截方式，不启动真实网关，不读登录态。线上热榜稳定性要另做一次低频率人工确认，不能靠这次两次探测下结论。

## 7. 风险

公开热榜是未文档化接口，字段和是否免登录都可能变。`label`、`sentence_tag`、`event_time`、`video_count` 的产品含义都还没和官方页面核对。`sentence_id` 能否跨改名追踪，也还不知道。嵌套视频对象不该落库。

`hot_value` 与搜索指数量纲不同，不能直接相加。创作者中心响应里的 `hot_list` 是综合指数，和本方案的热搜榜不是同一对象。`series` 会为缺失日补 null 占位，热榜存储不要照搬成「未上榜 = 0」。

云端浏览器资料按现有授权落盘。热榜客户端运行在网关侧、不携带创作者 Cookie。若端点以后开始要求 Cookie 或签名，应停止并人工确认，而不是把登录态接到这条链路。

`tencent-enable-service.sh` 会打印 `access.json`。部署热榜时不要顺手执行它。`HUB_API.md` 关于「重启必须重新扫码」的句子与后文持久化说明不一致，实现时以代码和 systemd 模板为准。

外部热榜归档仓库这次没有核对许可证和字段。不能把它们的历史数据直接写入 `hot_snapshots`。

## 8. 待评审

以下各项尚未决定。实现对应行为前需要用户确认。

- **429 / 403 / 非 JSON 之后是否必须人工恢复。** 第一期已按「暂停直到 `POST /api/v1/hot/resume`」实现。恢复后不立即补抓，仍要等满间隔。若要改成下一小时自动再试，需要另一次确认。
- **飙升阈值。** 第二期已按上述初始值作为默认，并可通过环境变量或 `hot_rising_thresholds` 调整。热度绝对值仍是按千万级量纲定的起点，要用实际快照再标定。z 只使用当前快照之前、同一 `sentence_id` 的非空热度，样本标准差为 n-1；没有 `sentence_id` 时 z 为 null。第一张快照上的话题都会显示为新上榜，因为回看窗口里还没有更早记录。
- **未上榜是否作为 0 贡献。** 第三期不把未上榜记为 0。快照存在但词没有精确命中时，该词状态是「未上榜」，整组热榜分为 null。快照不存在时热榜分为 null，词的状态保持 null，不能写成未上榜。
- **子串是否算命中。** 第三期只在 `substring_hits` 里展示，不进入热榜分。
- **融合权重 0.7 / 0.3。** 第三期以此为默认，可用 `INDEX_HUB_GROUP_SEARCH_WEIGHT`、`INDEX_HUB_GROUP_HOT_WEIGHT` 或 settings `group_score_weights` 覆盖，页面显示实际权重。两项都先换成百分制再加权。
- **历史回填。** 是否引入外部按小时归档的热榜，待确认。确认前要单独审查许可证、字段和缺测处理。第四期不自动消耗搜索指数 50 次额度。
- **网页端对照。** 已定 snssdk 为主源。网页端地址在本次实测中词序与主源一致，但是否长期作为对照探测，待确认。对照请求同样不入库、不占用搜索指数额度。
- **热榜日成功上限。** 60 分钟一次对应每天最多约 24 次成功抓取。48 次只是留余量的建议，待确认后写入单独计数器。
