# 百度搜索指数

百度热搜默认依次采集 `realtime`、`novel`、`movie`、`teleplay`、`livelihood`、`finance`、`sports`、`new_entertainment`、`internation_news`、`challenge`、`car`、`game`。默认每轮请求 12 个 tab，至少保存一个榜单时计为一次每日成功。`drama`、`documentary`、`homepage` 不在默认调度中；可用 `INDEX_HUB_BAIDU_HOTLIST_TABS` 覆盖已知 tab，homepage 始终不采集。

百度使用独立的有头 Playwright Chromium 持久 profile，由桌面进程按需启动并在空闲 300 秒后关闭；`INDEX_HUB_BAIDU_IDLE_SECONDS` 可设置 1–86400 秒。默认资料目录是项目下 `.cloud-home/baidu-profile`，可用 `INDEX_HUB_BAIDU_PROFILE_DIR` 指定其他服务账号所有的私有目录。目录 0700、文件由 077 umask 限制，拒绝不安全的路径、符号链接和非本用户所有的资料；不要提交、上传或打印浏览器资料。百度与抖音 profile 和浏览器进程分开。profile 近似明文存放登录态，能够读取资料的系统用户可以获得该账号登录态。

管理员在 Hub 查询对话框点击「打开百度登录窗口」，在中枢显示的二维码上扫码；二维码未就绪或失效时可刷新图片，远程桌面仍可作为备选。图片只截取百度登录页面识别到的二维码元素，在内存中传递；百度页面结构变化可能需要更新定位。登录状态使用浏览器内 Cookie 名称和有效期检查，不传出 Cookie 值；登录状态和官方签名脚本尚未在线验证，状态为 `signature_unavailable`、登录失败、验证、异常访问或限流时停止并处理。查询在官方页面中生成签名、同源请求搜索指数和 ptbk 并在页面内解码；仅日序列和状态通过私有 Unix bridge，Cookie、签名、解码表和原始正文不进入 Hub、结果或日志。单步超时 25 秒，响应最多 1 MiB，不跟随重定向、不自动重试。页面改版或风控可能使此链路失效。

`platform=baidu` 接受全国 1–5 个唯一词、最近两年最多 731 天，默认七天；每片至多 365 天，严格检查日期、日粒度和点数。整体、PC、移动三设备分开保存，界面默认展示整体；多词时可同图比较，设备切换和多词读取只使用已存数据。网页导出默认合并三设备和选中词；单词 API 的 `device=all_devices` 返回三设备，`all|pc|wise` 仍可单独读取或导出。缺失保持 null，指数不等于搜索次数。关联词和画像按单词读取。

`platform_limits.toml` 的 `[baidu]` 可由 `INDEX_HUB_PLATFORM_LIMITS` 指向另一份文件覆盖。默认单并发、逻辑查询间隔 20 秒加 0–10 秒抖动、分片间隔 8 秒、上海自然日 120 次逻辑查询和 400 次数据 HTTP；每片两次数据 HTTP 在派发前保守预占，缓存命中不计数。每步发送前核对预占的上海日期，午夜前 30 秒及跨日时停止，避免次日请求占用前一日预算。10001 至少冷却 3600 秒且须人工恢复；登录、验证、异常访问、签名不可用及未知错误暂停，失败不重试。非法配置回退默认并告警一次；间隔至少 5 秒、每日上限至多 1000、冷却至少 3600 秒。这些是试行工程保护，不是官方配额，不能保证账号不会受限。抖音额度不变。

## 部署与验收

合并后安装 `requirements-hub.txt` 和对应 Playwright Chromium；桌面启动脚本默认使用 `/opt/ms-playwright`，可用 `PLAYWRIGHT_BROWSERS_PATH` 覆盖。安排重启 gateway 和 desktop；重启会中断正在进行的登录或任务。先评估并提高桌面服务内存上限：先前调研的既有桌面约 2.93 GB，不是本轮实测；5 GiB 是以现有约 2.9 GB 加约 1–1.5 GiB 浏览器增量及余量推算的初步验收规划值，并非容量保证。应在实际部署环境测 cgroup 峰值和机器余量后调整。此处不修改 systemd 单元，也不执行部署。

真实验收由用户完成：在中枢或远程桌面扫码，选择官方页面已收录的一个词，独立记录连续七天每一天的页面值；明确页面最新可用日后提交一次采集，将结果与独立记录逐日核对，只有 7/7 一致才通过。缺失、登录失效、验证、限流或任何异常立即停止，不自动重试；不能把 API 返回值当作页面观察。离线测试不能证明真实登录、签名格式、平台响应或账号安全。

空闲关闭后状态为 `browser_idle`；格式校验失败时服务日志仅记录有限字段名、类型和数组长度，不记录字段值。

## 关联需求（试验）

关联需求使用 WordGraph 需求图谱，返回的 related_period 通常约为一年；Hub 仅对百度展示该窗口，不套用抖音关联词的截止日延迟限制。单次一个词、一次 HTTP 请求；唯一列表展示为搜索关联词，内容关联词为空。仅提取词语，数值字段暂未确认；指数不等于搜索次数。协议和结果尚待真实页面核对；页面和历史显示核对状态。

百度人群画像与地域已试接入，尚无本次线上验证。每次单词查询依次请求性别年龄、兴趣和地域三个接口；任一接口失败即停止，不重试。优先使用匹配词条，仅在没有匹配词条时采用唯一的「全网分布」。比率和地域热度单位待核实，地域热度为相对值；周期仅在地域接口返回且校验通过时记录，并在页面标出；原值不编造单位，核对状态单独展示。

### 资讯指数（默认关闭）

网关仅在 `INDEX_HUB_BAIDU_FEED_ENABLED=1` 时随搜索指数采集资讯指数；其他值或未设置均关闭。每个日期分片在搜索成功后额外调用 `/api/FeedSearchApi/getFeedIndex` 和该响应对应的 `/Interface/api/ptbk`，与搜索共用同一 120/400 工程额度，预占增加 2 HTTP/分片（共 4；关闭时仍为 2）。请求协议来自公开资料，完整结果尚未用真实账号验证。结构错误、登录、验证或限流使整个任务失败并按现有规则暂停，不自动重试；可识别的空序列保存为缺失，不补 0。

词条身份兼容 `key` 字符串及搜索接口同款数组 `[{name, wordType}]` / `[[{name, wordType}]]`，也兼容 `word`/`name` 原有形状，仍须等于查询词且不重复。只读日志已见 `index[].key` 为 `[{name, wordType}]`，其中 `wordType` 为 number，但看不到 `name` 词面值及 `wordType` 是否为 1；官方页面数值仍待核对。

开关打开时，24 小时内的完整缓存若无资讯指数则不命中，将重新采集搜索和资讯指数。

资讯指数是全国日指标，不分 PC/移动，与搜索指数口径不同，也不等于搜索次数；结果待官方页面独立核对。关闭采集后已存数据仍可读取、导出。页面切换只读取数据库，不触发采集。

上线开关需 root 写 systemd drop-in 并执行 daemon-reload，再重启 gateway 与 desktop（页内脚本在 desktop）。devbot 的免密 sudo 不能写 `/etc/systemd` 或执行 daemon-reload，与热搜开关相同。重启 desktop 可能中断当前百度浏览器窗口；持久 profile 通常还在，但进行中的登录和任务会被打断。

### 媒体指数（默认关闭，待核对）

[百度官方 Helper](http://index.baidu.com/Helper/) 将媒体指数定义为百度新闻频道收录的互联网媒体报道中，新闻标题包含关键词的数量；它与搜索指数的来源及计算方法无直接关系，也不区分 PC/移动。官方 FAQ 称搜索指数太低时无法计算媒体指数；官方不提供开放 API。

请求路径与解析参考公开第三方 [qdata](https://github.com/longxiaofei/spider-BaiduIndex/blob/master/qdata/baidu_index/extended_baidu_index.py)、[qdata common](https://github.com/longxiaofei/spider-BaiduIndex/blob/master/qdata/baidu_index/common.py)、[baidux](https://github.com/iamsk/baidux/blob/master/baidux/extended_baidu_index.py) 和 [抓包笔记](https://blog.csdn.net/CY19980216/article/details/107650145)：`GET /api/NewsApi/getNewsIndex` 使用 `word`、`startDate`、`endDate`、`area` 及现有页内签名，公开响应形状为 `data.uniqid`、`data.index[]`（`key`、`startDate`、`endDate`、加密 `data`），再用对应 `/Interface/api/ptbk` 解密。词条支持 `key`/`word`/`name` 字符串或单词条数组，必须匹配查询词、不重复，出现 `wordType` 时须为 1。真实响应形状、词条字段实际形态、官方页面数值和自定义地域是否真能出媒体指数均未用本项目真实账号核对；baidux 称不支持细分地域，官方帮助对自定义地域的描述同时提到三种指数。本实现仅全国、日粒度、整体。

网关仅在 `INDEX_HUB_BAIDU_NEWS_ENABLED=1` 时开启，其他值或未设置关闭；关联词和画像不采集媒体指数。每个搜索日期分片额外预占 2 HTTP，与搜索和资讯共用 120/400 工程额度：仅搜索为 2，加资讯或媒体为 4，两者都加为 6。资讯先于媒体；任一结构失败整个任务停止，不自动重试，空序列保存 missing，不补 0。24 小时完整缓存中，开启资讯须有 `feed_rows`，开启媒体须有 `news_rows`，两者同时开启须同时存在，即使全部 missing 也可命中。

`GET /api/v1/series?platform=baidu&keyword=…&metric=news_index` 和同参数 `/api/v1/export.csv` 只读取已存数据，`device` 仅可省略或为 `all`，CSV 的 metric 为 `news_index`；关闭开关后仍可读取及导出。结果待官方页面独立核对。上线更新需重启 gateway（读取开关）和 desktop（页内脚本），本次未部署。

百度失败日志记录可获得的请求阶段、HTTP 和业务 status、异常类型及已发请求数；无法确定的计数标为 unknown。消息只保留安全词汇，不记录 Cookie、密文或关键词。
成功时，资讯/媒体 extraIndex 的脱敏结构摘要写入 `index_hub.baidu` 的 info 日志（前缀 `baidu_extra_shape`）及结果 `metadata.extra_index_shapes`。摘要只含键名、数组长度、data/ptbk 长度、解密点数与是否回填，不含原文、密文、ptbk 值或词面。
