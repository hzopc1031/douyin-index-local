# 从 server-snapshot-2026-10-30 升级到 feature/douyin-hot-index

本分支在原指数工作台之外增加热榜快照、飙升计算和关键词组分数。升级只替换代码并重启 gateway。不要执行 `tencent-enable-service.sh`，它会把访问口令打到终端。

## 数据库

不需要单独的迁移脚本。gateway 启动并打开 SQLite 时，`hub_store.Store` 用 `CREATE TABLE IF NOT EXISTS` 补上：

- `hot_snapshots`
- `keyword_groups`

已有的 `points`、`jobs`、`datasets`、`insights`、`keys`、`settings` 不改结构，搜索指数和综合指数仍分列。`points.composite` 不会被热榜或关键词组写入。

升级前先停 gateway，复制备份。WAL 模式下不要只拷主库文件；停服务后复制主库，或用 SQLite backup API。默认库在 `INDEX_HUB_STATE_DIR/index-hub.sqlite3`，模板里的状态目录是 `/var/lib/douyin-index`。

## 要重启的单元

只重启 `douyin-index-gateway.service`。新路由、热榜调度和分数计算都在 gateway 进程里。

`douyin-index-desktop.service` 不用为这三期重启。热榜不走 Unix 桥，也不改浏览器资料目录。

`hub.html` 由每次请求从磁盘读取。只换页面时刷新浏览器即可；这次同时有 Python 变更，所以仍然要重启 gateway。

## 新增环境变量

都有默认值。不设置时，热榜调度保持关闭，飙升和关键词组仍可读取已有数据。

热榜调度：

- `INDEX_HUB_HOTLIST_ENABLED`：只有精确的 `1` 才抓取，默认关闭。
- `INDEX_HUB_HOTLIST_INTERVAL_SECONDS`：默认 3600，低于 1800 会抬到 1800。

飙升阈值：

- `INDEX_HUB_RISING_MIN_AGE_SECONDS` 默认 1800
- `INDEX_HUB_RISING_MAX_AGE_SECONDS` 默认 21600
- `INDEX_HUB_RISING_LOOKBACK_SECONDS` 默认 86400
- `INDEX_HUB_RISING_RANK_RISE` 默认 10
- `INDEX_HUB_RISING_RANK_MAX` 默认 15
- `INDEX_HUB_RISING_HEAT_PCT` 默认 0.5
- `INDEX_HUB_RISING_HEAT_DELTA` 默认 500000
- `INDEX_HUB_RISING_Z_MIN_SAMPLES` 默认 8
- `INDEX_HUB_RISING_Z_MIN` 默认 2

关键词组权重：

- `INDEX_HUB_GROUP_SEARCH_WEIGHT` 默认 0.7
- `INDEX_HUB_GROUP_HOT_WEIGHT` 默认 0.3

非法值回退默认。SQLite `settings` 里的 `hot_rising_thresholds`、`group_score_weights` 会按字段覆盖对应环境变量。确认要自动抓热榜时，只在 gateway 单元加上 `INDEX_HUB_HOTLIST_ENABLED=1`，然后重启 gateway。

## 建议顺序

1. 在服务器代码目录核对当前提交，确认仍是要离开的 `server-snapshot-2026-10-30`。
2. 停 gateway，备份 `index-hub.sqlite3`。
3. 取出本分支并安装已有依赖。不要新开服务账号，不要跑会打印 `access.json` 的脚本。
4. 先不要设置 `INDEX_HUB_HOTLIST_ENABLED`。
5. 启动 gateway。确认进程起来后，用只读请求看 `/api/v1/hot/latest`、`/api/v1/hot/rising`、`/api/v1/groups`。此时热榜应为空或只有以后抓到的数据，搜索指数的 `daily_online_attempts` 不应因这次升级增加。
6. 浏览器强制刷新工作台，确认热榜页和关键词组页能打开。
7. 需要自动抓热榜时，再打开上面的开关并重启 gateway。429、403 或非 JSON 会暂停热榜，要用 `POST /api/v1/hot/resume` 人工恢复。

## 回滚

1. 停 gateway。
2. 把代码切回升级前的提交（`server-snapshot-2026-10-30` 或当时记录的 SHA）。
3. 去掉这次为热榜或权重加上的 drop-in 环境变量，避免以后误读。旧代码会忽略不认识的变量，但不应留下已启用的抓取开关。
4. 启动 gateway。

旧代码不读取 `hot_snapshots` 和 `keyword_groups`，这两张表可以留在库里。不要为了回滚删除 `points` 或整库覆盖，除非备份确认需要整库恢复。整库恢复会丢掉升级之后新写入的搜索指数，只在确认要放弃这段数据时使用备份。
