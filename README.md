# 抖音搜索指数本地小批量验证工具

新增腾讯云运行版本：**指数工作台 / Index Atlas**，提供自用网页、SQLite 历史库、第三方 Bearer API Key、异步任务和 OpenAPI 文档。当前已接入抖音搜索指数；百度已接入独立浏览器取数，真实登录与逐日核对待验收；Google Trends 待接入。百度范围与限额见 [BAIDU.md](BAIDU.md)，操作和接口说明见 [HUB_API.md](HUB_API.md)。

新增实验入口：[外部设备登录、EC2 无浏览器单次查询](CLOUD_POC.md)。
`login_transfer.py` 在另一台电脑打开官方登录页，人用手机扫码；会话通过已验证的 SSH 直接进入 EC2 上 `cloud_probe.py` 的内存。EC2 不安装浏览器，只执行一次单词七天查询并清除会话。此入口已通过离线测试，尚未完成真实跨设备登录取数；不代表长期登录保持或登录设备断网运行已验证。

已在 2026-10-27 22:50（北京时间）完成一次 5 词批量真实取数。口径为全国、抖音、搜索指数，2026-10-19 至 2026-10-25，共 35 条日数据，与创作者中心折线图逐日提示值 35/35 精确一致。

| 关键词 | 完整日数据 | 与官方页面核对 |
|---|---:|---:|
| 酵素 | 7/7 | 7/7 一致 |
| 消化酶 | 7/7 | 7/7 一致 |
| 膳食补充剂 | 7/7 | 7/7 一致 |
| 银杏 | 7/7 | 7/7 一致 |
| 益生菌 | 7/7 | 7/7 一致 |

再次点击同一批查询已实测命中本地缓存，保留相同采集时间、结果目录及 35/35 核对记录，没有再次发送在线请求。原计划的「银杏提取物」在官方页面提示“尚未收录”，因此用「益生菌」替换；未收录提示已留截图，未将该词记作 0，也未对该词发起本地 API 查询。

## 启动与使用

源码仓库：[popsunlake/douyin-index-local](https://github.com/popsunlake/douyin-index-local)（私有）。已验证的桌面环境为 macOS、Python 3.12。新克隆不含 `.venv` 或已打包应用，先安装依赖：

```sh
git clone git@github.com:popsunlake/douyin-index-local.git
cd douyin-index-local
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./start.command
```

也可运行 `.venv/bin/python app.py`。`start.command` 优先使用本地已打包应用，没有打包产物时运行源码。已有环境可直接双击 `start.command` 或 `dist/DouyinIndexLocal.app`。应用依赖旁边的 `app.py`、`client.py`、`storage.py`、`ui-batch.html`，请保留整个项目目录结构。

1. 在「抖音创作者中心 · 官方登录与核对」窗口用抖音 App 扫码登录。
2. 进入「创作服务 → 抖音指数」，查询关键词，选择「过去7天」，确认全国、抖音与日期。最新可用日期由页面决定，不假定昨天一定可取。
3. 将官方窗口最小化，回到本地面板，每行填写一个关键词（1—5 个、不重复）与截止日，再点击「获取或读取这批 7 天数据」。本次验证在 10 月 27 日看到的截止日为 10 月 25 日。
4. 在「查看关键词」中切换词，将官方图表每一天的「关键词搜索指数」提示值录入本地表格，保存整批核对结果。切换词会保留输入，全部日数据均精确一致才登记整批通过。带「万」的缩写不会被判为精确一致。

完整结果在采集后 24 小时内可复用：同样的有序关键词列表、日期、平台和地区再次查询时直接读本地缓存，不再请求抖音，也无需登录。损坏、过期或不完整的数据不会命中缓存；采集时间会显示在结果中。这个缓存有效期是本工具的设置，不代表平台数据不会修订。

每次成功请求写入独立 `results/时间戳-随机后缀/`：

- `result.json`：接口原始业务响应、解码响应、标准日序列、质量信息和采集时间。
- `search-index.csv`：关键词、日期、搜索指数、综合指数；两种指标明确分列。
- `comparison.json`：逐日页面原文字、核对结果和核对时间。

本次批量成功结果在 `results/20261027-225022-716448-158a67d6/`。完整页面截图、35 条页面观察记录、缓存命中证据、结果副本及 SHA-256 清单位于工作区 `outputs/douyin-batch-verification/`。官方页面数值是在本地批量 API 请求之前独立读取的，并非从程序结果复制回填。

此前 22:20 的单词「酵素」验证仍保留在 `results/20261027-222037-225006/` 与 `outputs/douyin-verification/`，也为 7/7 一致。

上述真实采集结果和截图仅保存在原验证机器，未上传 GitHub。仓库包含源码、离线合成测试和验证结论，不包含账号 Cookie、浏览器资料、会话文件、原始结果或打包产物。

## 在 ChatGPT Work 中继续

可以让 Work 读取仓库、修改源码、运行离线测试和检查改动。私有仓库需要当前 GitHub 连接具有访问权限；要在执行环境中运行测试，还需要能够拉取源码并安装 Python 依赖。支持范围取决于账号、工作区和任务所获工具权限。

- **本地 Work：** 选择这个项目文件夹，在有权限的 Mac 上运行源码，可以继续使用官方窗口扫码和人工页面核对。
- **纯云端 Work：** 可进行源码优化和离线测试；当前 macOS 窗口和抖音登录会话不会自动传到云端。可通过新增的显式 SSH 会话传送入口验证 EC2 取数，详见 CLOUD_POC.md；不能把离线测试通过当作线上取数通过。
- **Work Cloud 的本地电脑访问：** 如果账号/工作区已启用相应能力，且 Mac 在线、已连接，可以让需要本地资源的步骤在 Mac 上执行；不要仅凭任务名里有“Cloud”推断它实际使用哪台机器。

无桌面环境只安装核心依赖即可跑全部离线测试，测试不会访问真实抖音接口：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-core.txt
.venv/bin/python -m unittest -v test_client.py test_storage.py test_app.py
```

新增云端入口和登录助手的离线测试（不需要浏览器或真实 SSH）：

```sh
.venv/bin/python -m unittest -v test_cloud_probe.py test_login_transfer.py
```

建议接手提示词：

> 读取这个私有仓库的 README.md 和 AGENTS.md，安装 requirements-core.txt 并运行离线测试，再按我的需求修改代码。保留搜索指数与综合指数的区分、缺失不补零、缓存完整性检查、会话仅在内存中和不自动重试的约束。报告修改与测试结果；真实扫码取数在获准的本地 Mac 环境单独验证。

官方能力说明（核实于 2026-10-27）：[使用 ChatGPT：云端与本地 Work](https://learn.chatgpt.com/docs/use-chatgpt)、[Work 入门及本地电脑访问](https://learn.chatgpt.com/docs/get-started-with-work)。

## 实现与边界

`client.py` 独立实现 [douyin-index-tool 的接口协议](https://github.com/Ecow0ker/douyin-index-tool/blob/main/src/douyin_index_tool/api.py)，并参考其 [加密格式](https://github.com/Ecow0ker/douyin-index-tool/blob/main/src/douyin_index_tool/crypto.py)。核验时参考仓库主分支树 SHA：`3582f5bac532fce6d561b27e67494c984c08da6e`。该仓库未见明确许可证，未整套复制或运行其程序。

真实请求：`POST https://creator.douyin.com/api/v2/index/get_multi_keyword_hot_trend`。请求包含一个 `keyword_list`、`start_date`、`end_date` 和 `app_name=aweme`；本轮响应 HTTP 200、`x-encrypted=2`，解码后包含 `hot_list` 与 `BaseResp`。搜索指数取 `search_hot_list[].index`，综合指数取 `hot_list[].index`。

登录窗口使用 [pywebview](https://pywebview.flowrl.com/api/) 的私密模式；仅将当前窗口的抖音域 Cookie 在内存中交给本地 HTTP 客户端，请求结束后清除客户端 Cookie。不会读取 Chrome 的用户资料，也不把 Cookie 写入文件、日志或界面。关闭程序后需重新扫码。

只允许固定 HTTPS 端点；开启证书校验、禁止重定向、没有自动重试。验证码、限频、登录错误、格式变化均停止处理。缺失不补 0；错词、重复日期和范围外日期报错。每次启动最多发起一批线上查询，发生错误也不自动恢复；之后仍可读取缓存。输入和登录预检失败不会消耗查询机会。此限制用于本次小批量验证，不代表官方配额。当前限定每批 1—5 个词和包含首尾的七天。

本轮证明这个账号的上述 5 词、这个区间的一次批量请求与页面一致，并验证了缓存复用；未验证长期稳定性、平台批量额度或更早历史数据。搜索指数不等同实际搜索次数。

## 开发与验证

`requirements-core.txt` 固定核心依赖；`requirements.txt` 额外安装桌面窗口依赖。运行：

```sh
.venv/bin/python -m unittest -v test_client.py test_storage.py test_app.py
```

47 项离线测试通过，覆盖两种加密格式、HTTP/业务错误、验证提示、无自动重试、日期解析、缺失、重复、错误关键词、七天约束、缓存完整性及批次核对。单词与五词批量版本均已完成真实端到端逐日核对，证据分别留存。

修改 Python/HTML 源码后重新启动即可，原生启动器运行项目中的可检查源码。若需重新打包，在 macOS 运行 `.venv/bin/python -m pip install pyinstaller==6.22.3` 后执行 `./build.command`。打包脚本仅在 Python 运行时包含 `codex-rejected-cas.pem` 时把它带入产物，普通 Python 无需该专用文件；证书验证始终开启。

可运行无登录凭据的连接自检：

```sh
dist/DouyinIndexLocal.app/Contents/MacOS/DouyinIndexLocal --connection-check
```

云端 Hub 默认持久化浏览器登录资料到服务账号所有的 0700 目录：`INDEX_HUB_PROFILE_DIR`，默认 `INDEX_HUB_STATE_DIR/browser-profiles`。Cookie 以近似明文形式保存在该目录（无系统钥匙串），任何能以 douyin-svc 或 root 身份读盘的人都能拿到登录态；若目录不归 douyin-svc 所有或是符号链接，app.py 会拒绝启动。禁止提交、上传或打印这些资料。`INDEX_HUB_PERSIST_LOGIN=0` 恢复无痕模式；普通桌面启动仍使用无痕模式。远程桌面先由管理员携带 `X-Hub-Request` 调用 `POST /api/v1/desktop/ticket`，取得 60 秒内单次有效的 WebSocket 票据（仅内存保存其 SHA-256）。

短信方案 B 的底层组件在 `login_driver.py`、`sms_login.py`：后台状态机、Qt 主线程截图与按键、QTimer 鼠标回放、短信/扫码/取数互斥。手机号、短信码只在内存中处理，不写日志、SQLite 或结果文件；截图遮盖输入框，快照只显示脱敏手机号（如 `138****8000`），验证码不回显。已接通短信桥接、管理员网关与手机号登录卡片，并加入本地假页面的完整 Qt 回归（方案第 12 节第 1–7 项及本地滑块扩展）。Hub 启动脚本和部署模板默认 `sms`；两服务均可设 `INDEX_HUB_LOGIN_MODE=qr` 或 `desktop` 切回。直接运行普通桌面仍默认 desktop，8443 不变。短信期间暂停采集，成功、失败、取消或超时后仍需手动恢复；没有自动重试。接口与状态字段见 [HUB_API.md](HUB_API.md)。

不设手机号白名单。start 至少间隔 10 秒，发码倒计时至少 60 秒，每小时最多 3 次、每上海自然日最多 6 次，只记次数不存号码；pointer 每秒最多 3 次，text/code 各每轮最多 5 次。每轮最多 10 分钟，code_sent 空闲最多 5 分钟，每次发码最多提交 3 次，由 per-send nonce 和 code_consumed 拒绝重复/旧代提交。这些是本服务的保护限制，不是平台官方额度。

短信状态机在准备操作时拒绝非官方 HTTPS 来源；Qt 在每次按键、点击和拖动回放时复核窗口来源，并在输入期间发生导航时中止操作。登录验证结束后出现错误或过期短信码会恢复验证码输入，仍保留每次发码 3 次提交、重复码和重发倒计时限制。Unix 桥接并发处理等待中的拖动请求，状态查询和取消不受长拖动阻塞；浏览器取数与登录互斥锁保持有效，取消中的拖动返回 `cancelled`（HTTP 409）。

新一轮开始时若页面还留有上一轮的安全验证，Qt 最多重新打开一次当前可信来源页面；只有重新观察到发码入口后才按原限额发起新发送、生成新 nonce。遗留验证或旧倒计时不能被当作本轮已发码，页面重置失败则停止。人工拖动后的 3 秒结果等待只属于原题；同一验证阶段推进到新题会清除旧等待，截图动画或同题布局变化不会延长等待。

可选本地识别器使用 MIT 的 `captcha-recognizer==1.0.2`（依赖 ONNX Runtime），只处理明确识别为普通缺口滑块且几何信息完整的题目。安装 `requirements-solver.txt`；模型随该版本 wheel 提供，无运行时下载，也不把模型加入仓库。依赖/模型按需加载，禁用 ORT 遥测，不调用第三方服务。每轮最多自动提交 3 次，两阶段共用计数；只在新题身份稳定后尝试，不因动画重试。低置信度、异常、拒绝或页面未确认通过均退回人工截图，人工拖动仍未通过则提示远程桌面；旋转题直接提示远程桌面。出现平台限频、账号异常或拒绝访问立即停止。

`VerificationSolver.solve(capture, challenge, cancel)` 是扩展点：返回相对坐标轨迹或 `None`，不直接操作浏览器；`solver=None` 可禁用自动识别。状态机执行次数限制、取消检查、重新探测以及与人工共用的回放。页面仍须独立确认倒计时/登录成功。截图 `frame_id` 表示图片更新；服务端另保留题目/布局 epoch，同一 epoch 保留最近 5 张图片的坐标元数据，超出范围或换题、裁剪、缩放变化则拒绝。题目身份只对配置的 DOM 资源属性做内存摘要，不返回资源 URL。所有短信选择器、题型文字、身份属性均集中于 `SMS_SELECTORS`。登录/二维码 DOM 已按 2026-10-31 公开页面校准；滑块尚未见到真实页面，其几何/身份规则仍待验证。跨域 iframe 或无可识别身份/几何信息的题目不能承诺自动处理，canvas 内换题也需要校准可观测的身份属性。

此次校准没有输入手机号或发送短信。页头「登录」打开顶层弹窗，可能跳转 `/jingxuan`；验证码登录默认激活，不重复点击页签，国家区号保持 +86。手机号/验证码使用稳定 ID、name 和 placeholder；发码控件为 span，提交为 div，等待启用后点击；没有协议勾选框，仅有隐含同意文字。弹窗支持 `login-full-panel-` ID 前缀回退，扫码回退支持 `douyin_login_comp_scan_code` 与 `animate_qrcode_container` 内 `aria-label="二维码"` 的图片。忽略隐藏辅助 iframe，检测可见 verifycenter/verify iframe 或覆盖层并保留原 CAPTCHA 规则。短信输入仅接受 HTTPS `creator.douyin.com` / `www.douyin.com` 默认 TLS 端口；采集仍要求创作者中心页面。自动识别逻辑、限额和登录模式开关不变。

页头跳转时，即使表单已可见，也须等到文档和 Qt 导航均完成、探测期间未换页，才允许开始输入或占用发码额度；等待保留取消和超时限制。若认证完成后仍停留在 `www.douyin.com`，短信流程只返回一次固定创作者指数页，等待加载完成并重新检查登录表单和内存会话后才报告成功。返回失败、取消、超时或创作者页仍未就绪均不会报告成功，也不会循环跳转或再次发码；采集来源限制和成功后手动恢复保持不变。

离线验证（本次测试环境 Python 3.13；项目常规环境仍为 3.12）：

```sh
/tmp/dyvenv/bin/python -m pip install -r requirements-solver.txt
INDEX_HUB_REQUIRE_SOLVER_TESTS=1 /tmp/dyvenv/bin/python -m unittest -v test_slider_solver test_sms_login
INDEX_HUB_REQUIRE_BROWSER_TESTS=1 INDEX_HUB_REQUIRE_SOLVER_TESTS=1 xvfb-run -a /tmp/dyvenv/bin/python -m unittest discover -p 'test_*.py'
/tmp/dyvenv/bin/python measure_solver_rss.py
INDEX_HUB_REQUIRE_BROWSER_TESTS=1 INDEX_HUB_REQUIRE_SOLVER_TESTS=1 xvfb-run -a /tmp/dyvenv/bin/python measure_solver_rss.py --qt
node test_report_ui.cjs
node test_sms_ui.cjs
```

模型测试只用程序生成的合成缺口图。未安装可选依赖/模型时跳过；`INDEX_HUB_REQUIRE_SOLVER_TESTS=1` 将缺失视为失败。Qt 输入测试仅在 `INDEX_HUB_REQUIRE_BROWSER_TESTS=1` 时执行，保留真实 `isTrusted`、坐标、节奏与缩放失效断言。JS 摘要/矩形通过 JSON 字符串跨过 PySide 回调边界，避免绑定版本对对象/数组返回值的转换差异。

`fake_sms_page.py` 的 aiohttp 页面仅由测试绑定 127.0.0.1：登录结构模拟此次公开 DOM，包括页头打开与 `/jingxuan` 跳转、并排二维码/默认短信面板、密码页签、span/div 控件和隐藏 verifycenter；另有缩短倒计时、合成缺口图、错误码提示、假 sessionid，以及旋转、限频、二次验证开关。`test_sms_browser.py` 走真实管理员 HTTP、Unix 桥接、后台状态机和 Qt 按键/鼠标；实际模型识别截图后必须由页面确认通过。覆盖人工拖动、跨两个阶段累计 3 次自动提交后转人工、人工失败/旋转转远程桌面，并拒绝平滑但 `isTrusted=false` 的 JS 事件。完整成功流程及退出后扫描临时目录、Python/Qt/Chromium 日志、SQLite/WAL、数据库逻辑导出和所有 API 响应；同时检查输入框截图遮盖。浏览器使用临时无痕 profile；只有测试适配器映射本地来源以验证成功判定，生产不接受回环来源。

RSS 脚本默认测 ONNX 模型及一次合成识别；`--qt` 在同一进程加载 app/SMS/模型、完成 Qt 登录及泄露检查，再识别一次合成图。输出进程峰值和每 50ms 采样的含 Chromium 子进程 RSS 总和（共享页会重复计数，采样可能错过短峰，非 cgroup 内存值）。本地小页面的内存占用不能代表真实官方页面或部署服务峰值。内存测试不提供真实登录、滑块校准或线上通过率结论。

2026-10-31 本地验证（Python 3.13、QtWebEngine 6.12.0、pywebview 6.2.1）：上述双标志全量 unittest **362 项通过、0 跳过**；两个 Node 脚本通过（SMS 12 项，report 47 条断言）。新增 6 项真实 Qt 短信回归，可信事件和泄露检查通过。独立 ONNX 识别进程峰值 **342.7 MiB**；app/SMS/模型加 Qt 完整流程进程峰值 **684.2 MiB**，含 Chromium 的采样进程树峰值 **897.8 MiB**，低于模板的 3072 MiB 硬上限。未部署、未发真实短信、未消耗取数额度。

同日代码审查修复后：新增 13 项 Python 回归及 1 项短信 UI 回归，双标志全量 unittest **375 项通过、0 跳过**；SMS UI **13 项通过**，report **47 条断言通过**。覆盖非官方来源与输入中导航、登录验证后的短信码过期，以及 8 秒拖动期间的状态查询、取消、worker 轮询和浏览器互斥。仅使用合成数据及回环页面。

同日公开 DOM 校准后：本次新增 7 项回归（3 项状态机、4 项真实 Qt），覆盖页头打开与跳转、默认短信页签、稳定选择器回退、span/div 禁用状态，以及隐藏/可见 verifycenter；原有完整 Qt 流程改用此次 DOM 结构。双标志全量 unittest **397 项通过、0 跳过**；report UI 通过，SMS UI **19 项通过**。测试仅使用合成数据与 127.0.0.1，未部署、未发真实短信，滑块仍未见到真实页面。

同日导航审查修复后：新增 9 项回归（返回创作者来源 5 项、等待导航完成 4 项），另加强既有采集来源检查；真实 Qt 测试分别阻塞创作者页加载和页头跳转后的子资源加载，验证成功前不可恢复采集、导航完成前不输入或占用发码额度。浏览器状态测试通过实际 Unix 桥接及生产来源检查，完成登录后手动恢复成功。按上述双标志命令，Python 3.13.5 全量 unittest **406 项通过、0 失败、0 跳过**；report UI **47 条断言通过**，SMS UI **19 项通过**。仅合成数据与 127.0.0.1，无真实登录或线上取数验证。

## Hub 范围选择与紧凑结果（2026-11-02）

网页默认显示关键词概要：最新日期的搜索指数/综合指数、前三搜索关联词，以及性别/年龄/省份等维度的最高原值分组。通过「趋势」「关联需求」「人群特征」「历史」页签查看详情；关联词与画像采用本地 CSS 条形图，每块默认前五条，「展开全部」保留全部原始指标和 TGI。缺失值仍显示 `—`，条形只比较原值，不表示概率或百分比。图表未新增人工核对结论。任务进度位于结果下方，默认折叠；展开后可处理未确认任务。

「新建完整查询」打开查询面板。「查询范围」与只读的「展示范围」均支持最近 7 天、30 天、3 个月、6 个月、1 年和自定义起止日期。快捷段以当前选定的结束日期为锚点，含首尾；月份/年份按日历回退，月末夹到目标月末，再加一天（例如 2026-10-31 的近 3 个月为 2026-08-01—2026-10-31）。在线查询的近两年及最多 731 天限制均保留，非法范围有提示并阻止提交；只读展示仍可回看更早的已保存趋势（每段最多 731 天）。初始查询与展示范围取本地已存有效搜索指数的最大日期，截至该日最近 30 天，不继承最近一次采集的日期段。这个日期只是本地已知可用日，不代表官方当前最新日；没有有效日值或旧网关未提供该字段时留空，须从官方页面确认。轮询不覆盖手动选定日期，未提交/未确认批次保留原输入，已完成旧批次不覆盖初始范围。API 不填开始日期时仍默认七天。

离线前端验收（安装 `requirements-qr.txt` 及对应 Playwright Chromium）：

```sh
.venv/bin/python check_hub_ui.py --screenshots /workspace/content/dev
node test_report_ui.cjs
node test_sms_ui.cjs
INDEX_HUB_REQUIRE_BROWSER_TESTS=1 INDEX_HUB_REQUIRE_SOLVER_TESTS=1 xvfb-run -a .venv/bin/python -m unittest discover -v -p 'test_*.py'
```

`check_hub_ui.py` 将每个请求拦截到合成 fixture，不启动网关，不读取登录态，也不访问真实查询接口。检查 430×932 与 1280×900 的首屏概要、快捷范围/月末/闰日/731 天边界、键盘页签、展开全部、缺失值、短屏弹窗校验提示以及 mock 提交的原有请求结构。截图仅写入指定目录，勿提交截图。

**仅此次前端变更上线无需重启网关。** `hub_web.py` 的 `mount.index` 每次请求创建 `web.FileResponse(hub.root / 'hub.html')`，文件没有启动期内容缓存。aiohttp 根据磁盘文件时间/大小生成 ETag 和 Last-Modified，可返回 304；文件更新后新的请求读取新文件。已用同一个本地合成网关实例替换 HTML，并携带旧 ETag 请求，确认得到 200 和新内容。已打开的页面需重新加载；浏览器仍显示旧版时强制刷新。

用户验收并批准上线后：在服务代码目录核对基线及工作树，备份当前 `hub.html` 到仓库外受控位置；将已验收提交的 `hub.html` 以同目录临时文件加原子替换的方式更新，保留服务可读的属主/权限；重新加载网页，检查范围和已保存结果即可，无须提交在线任务或重启 gateway/desktop。失败时原子恢复旧 `hub.html` 并刷新。部署路径见 `deploy/systemd/douyin-index-gateway.service` 的 `WorkingDirectory`；此次不改任何服务配置或浏览器资料。

## 人群空态与已存日期段（2026-11-02）

人群特征区分「未采集」「平台无数据」「采集失败」「趋势无数据已跳过」，
概要同步显示原因；排队/运行中及读取错误另外展示。未采集仅表示当前范围没有保存结果，
不据此推断从未发起过任务。「前往新建完整查询」打开现有查询面板，预填当前词与日期，
仍需用户点击提交；存在未确认批次时保留原批次输入。

已存人群日期段可显式点选，切换所有结果模块的展示范围；没有自动选择其他范围或自动补采。
切换词、日期或平台后，迟到的成功/失败响应不能覆盖当前选择。缺失指标保留为 `—`。

上线可以分两部分：

- **HTML 修复无需重启**：只更新 `hub.html` 并刷新网页。旧网关使用现有只读接口，
  列出最近 30 条任务、本会话及当前范围已确认的日期段，并说明更早记录可能遗漏。
  此时「未采集」还提示无法排除更早任务曾失败/跳过。
- **完整历史列表需重启 gateway**：`hub_store.py`、`hub_web.py` 新增只读
  `GET /api/v1/insights/ranges`，列出全部已存日期段及每段最近任务状态，
  无数据库迁移，不需要重启 desktop。`hub_insights.py` 和日期校验另将只读历史与在线限制分开，
  过了近两年的已存范围仍可读取，在线提交仍限近两年。现有接口没有全历史分页能力，这是新增后端的原因。

离线验证新增 `.venv/bin/python check_audience_ui.py --screenshots /workspace/content/dev`，
覆盖 430/1280px 四种空态、两段切换、完整列表/旧网关回退、过期响应和打开查询但不提交。
所有网络都由合成 fixture 拦截；截图不是服务器数据或人工核对证据。

## 中枢查询范围与只读历史修复（2026-11-02）

扩展任务只在同平台、同关键词、同起止日期的最近 24 小时趋势确认无数据时跳过。
趋势批次可以包含其他词；匹配按扩展任务实际使用的日期（关联词可能按现有规则截短）。
同段已有非空搜索指数时保留原有保护，不跳过；其他日期的观测不参与判断。
跳过任务的 `error.source_trend` 保存来源趋势 job ID、平台、词和起止日，不消耗在线次数。

`GET /history` 只读数据库，连 Bearer 密钥使用时间也不更新；旧桌面结果在网关启动时导入，
运行中新落盘的桌面结果等下次启动才导入。Hub 任务完成后直接入库，刷新历史不再触发导入。
返回的 `latest_available_date` 是抖音搜索指数按官方 T+1 推算的默认截止日，
不再用本地已存最新搜索日，以免默认日期自锁。采集失败时回退到本地已存日。
Store 中的本地最新日仍从全部已存非空搜索日值计算（包括真实零值），
不受历史列表最近 30 组或采集时间排序影响；缺失值及仅有综合指数的日期不算可用搜索日。

这三项修复需要一起更新后重启 gateway；desktop 无需重启，无数据库迁移，不重试旧失败任务。
已打开网页需刷新。重启会短暂中断网页/API/远程桌面代理；须避开登录交互和运行中任务。
离线回归另有 `node test_date_ui.cjs`，覆盖最新已知日、30 天、旧网关/空库、手工日期与未确认批次。
