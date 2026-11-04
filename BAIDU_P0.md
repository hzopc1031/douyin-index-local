# 百度指数 P0（B1-BAIDU-P0-2340）

仅内部分析；本次实现、验证均离线。百度路由可接受任务，但生产桥接始终返回“未登录 / 待登录”，没有真实数据请求实现，不能据此声称已上线或已登录。没有部署或重启服务。

## 范围与协议

- 全国（area=0）、整体搜索指数（all）、单词、日粒度、连续七天。截止日由官方页面确认；新任务仅接受最近七日内的过去截止日，不假定昨天可用。已存历史允许只读查询。
- 自写 `baidu_adapter.py`，依据本地 2026-11-02 调研中 Qdata 协议事实；未复制其代码，未引入采集 SDK。参考版本 [fe251d22](https://github.com/longxiaofei/spider-BaiduIndex/blob/fe251d22a9938ee9f0bd667ad153cd9f1da20fef/qdata/baidu_index/common.py)。本期未重新访问上游源码或百度。
- GET `https://index.baidu.com/api/SearchApi/index`，word 为二维词组 JSON，带 area/startDate/endDate；得到 uniqid 后 GET `/Interface/api/ptbk?uniqid=...`。字符表前半映射后半；解码后严格校验词、七日日界、粒度和长度。重复 JSON 键、重复/额外词、错日期、非数字、非法映射均停止。
- 10000 → 未登录，10001 → 受限，10018 → 缺 Cipher-Text；HTML → 人工验证；未知业务/HTTP/格式/网络错误均停止，无重试。错误只有固定安全摘要，不转发正文或异常文本。
- 空序列/空 token 为 `missing`、null；不根据空列表推断未收录。`page_absence` 只接受未来浏览器独立观察到的 `unindexed` / `low_volume`，也保存 null。本期没有对应页面识别器，只验证合成 fixture。明确数值 0 仍是数值 0，不把它猜成低量；缺失、未收录、低量永远不补 0。
- userIndexes/word/all 的结构和当前 Cipher-Text 生成尚待真实页面校准；Cipher-Text 不自写、不复用静态密钥。当前严格 schema 可能拒绝实际响应，届时需以安全的脱敏结构证据修订并加 fixture。

## 会话与登录骨架

复用现有管理员 `/desktop/`、noVNC 和 `baidu_window.py`。它是独立进程，`private_mode=True`，不调用抖音持久 profile 工厂，不提供 Python API bridge，不开启调试端口。用户未来在百度官方页面完成扫码/密码/短信/挑战；本期不打开窗口、不实现登录协议、不持久化百度 profile。进程重启须重新登录。Hub 的抖音登录卡片仍只管抖音。

`BrowserTransport.get(url)` 是浏览器侧接口契约，P0 没有实现。后续必须留在该独立浏览器上下文中，使用官方脚本的动态请求头，TLS 验证、禁止重定向/重试、每步总期限 25 秒、响应不超过 1 MiB；两步共用会话，中间不额外等待 60 秒。原始响应和 ptbk 只在浏览器侧解析；0600 Unix bridge 只回传投影数据和状态。Cookie、BDUSS、Cipher-Text、ptbk、原始正文不进入 Hub API、配置、日志、聊天或结果文件。

现有 `baidu_status` / `baidu_query` 分支是 fail-closed 桩，没有把临时窗口接通；接口可用和真实取数启用分别由 `available` / `live_enabled` 表达。P0 `live_enabled=false`，不能通过提交参数或环境变量开启。真实接线是后续工作，需要补充浏览器请求白名单、超时关闭/人工恢复、页面/登录流量上限及泄漏测试后才能启用；当前数据 HTTP 预算不覆盖整个浏览器网络。

## 中枢隔离与持久数据

`hub_baidu.py` 复用 Hub 队列/SQLite/0600 bridge 机制。百度使用 `baidu.*` 设置，单并发，逻辑尝试间隔至少 60 秒，20 次/上海日，60 次数据 HTTP/上海日。两步请求在派发前以一个事务预占 1 次逻辑尝试及 2 次 HTTP；未知失败/超时/中断不退还，宁可少用预算，不低估流量。当前仅两步时，20 次逻辑上限通常先触发。缓存及未登录预检不消耗预算。

失败暂停百度，不自动重试；重启时正在执行的百度任务标失败并保留独立暂停及预算。管理员 `POST /api/v1/collection/resume` 可传 `{"platform":"baidu"}`；必须通过百度浏览器就绪检查，P0 桩不能恢复。手动恢复不会重置预算或重放失败任务。抖音原有 15 秒/50 次、暂停/恢复及普通桌面行为保持原样，百度冷却或暂停不阻塞抖音任务。

结果含 `platform/metric/device/region/granularity/source/fetched_at`。仅百度添加 `baidu_points` 表（不改写抖音 points），主键含全部固定业务维度、词与日；fetched_at 是采集版本，不属于缓存身份。缓存 hash 包含所有业务维度和查询范围，只有完整日值能命中，24h 保持不变。保存数据/series/CSV 均保留 null 和 value_status，CSV 不把百度标成抖音。百度不接受关联词/画像请求。`IndexHubClient` 的 query/series 可显式指定 `platform='baidu'`，默认仍抖音。

## 离线验证与下一次真实验收

测试只用合成 fixture、临时 SQLite、回环 HTTP 和私有临时 Unix socket；包含两步失败不重试、严格解析、结果投影、并发、两种预算、跨日、重启、平台隔离、缓存、CSV、API、临时窗口参数。Node 测试验证百度只创建指数任务，抖音仍创建三个模块。

```sh
python3.12 -m unittest -v test_baidu_adapter test_baidu_hub
node test_report_ui.cjs
node test_baidu_ui.cjs
python3.12 check_baidu_ui.py
```

真实验证需后续明确授权并完成浏览器传输：由用户选择本人可用、已绑定可接收验证手机的普通百度账号，准备百度 App；通过现有远程桌面登录，不提供账号密码/Cookie 给 Hub 或聊天。选一个官方页面已收录词，确认全国/整体与官方截止日，独立记录 7 个逐日页面值，再显式发起一次逻辑采集（预计 SearchApi + ptbk 共 **2 次数据 HTTP**，不另发收录检查请求）。页面/登录自身流量另计且事前不可精确估算，不宣称只有两次总网络请求。任何登录、验证、受限、缺头、格式异常立即停；不自动重试。只有 7/7 独立页面观察匹配，才记录真实验证通过。

评审修正：旧 `query` 桥接命令拒绝百度参数，防止误走抖音；前端恢复按钮明确区分平台，校验百度截止日并允许修正已明确拒绝的参数；切平台时清理导出和旧信息，拒绝迟到的异平台图表响应。均使用离线回归覆盖。
