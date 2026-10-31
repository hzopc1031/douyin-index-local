# 抖音搜索指数本地小批量验证工具

新增腾讯云运行版本：**指数工作台 / Index Atlas**，提供自用网页、SQLite 历史库、第三方 Bearer API Key、异步任务和 OpenAPI 文档。当前已接入抖音搜索指数；百度与 Google Trends 明确显示待接入。操作和接口说明见 [HUB_API.md](HUB_API.md)。

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

可选本地识别器使用 MIT 的 `captcha-recognizer==1.0.2`（依赖 ONNX Runtime），只处理明确识别为普通缺口滑块且几何信息完整的题目。安装 `requirements-solver.txt`；模型随该版本 wheel 提供，无运行时下载，也不把模型加入仓库。依赖/模型按需加载，禁用 ORT 遥测，不调用第三方服务。每轮最多自动提交 3 次，两阶段共用计数；只在新题身份稳定后尝试，不因动画重试。低置信度、异常、拒绝或页面未确认通过均退回人工截图，人工拖动仍未通过则提示远程桌面；旋转题直接提示远程桌面。出现平台限频、账号异常或拒绝访问立即停止。

`VerificationSolver.solve(capture, challenge, cancel)` 是扩展点：返回相对坐标轨迹或 `None`，不直接操作浏览器；`solver=None` 可禁用自动识别。状态机执行次数限制、取消检查、重新探测以及与人工共用的回放。页面仍须独立确认倒计时/登录成功。截图 `frame_id` 表示图片更新；服务端另保留题目/布局 epoch，同一 epoch 的旧图片允许回传坐标，换题、裁剪或缩放变化则拒绝。题目身份只对配置的 DOM 资源属性做内存摘要，不返回资源 URL。所有选择器、题型文字、身份属性均集中于 `SMS_SELECTORS`，尚未真实校准；跨域 iframe 或无可识别身份/几何信息的题目不能承诺自动处理，canvas 内换题也需要校准可观测的身份属性。

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

`fake_sms_page.py` 的 aiohttp 页面仅由测试绑定 127.0.0.1：含二维码/短信页签、缩短倒计时、合成缺口图、错误码提示、假 sessionid，以及旋转、限频、二次验证开关。`test_sms_browser.py` 走真实管理员 HTTP、Unix 桥接、后台状态机和 Qt 按键/鼠标；实际模型识别截图后必须由页面确认通过。覆盖人工拖动、跨两个阶段累计 3 次自动提交后转人工、人工失败/旋转转远程桌面，并拒绝平滑但 `isTrusted=false` 的 JS 事件。完整成功流程及退出后扫描临时目录、Python/Qt/Chromium 日志、SQLite/WAL、数据库逻辑导出和所有 API 响应；同时检查输入框截图遮盖。浏览器使用临时无痕 profile；只有测试适配器映射本地来源以验证成功判定，生产 HTTPS/域名检查不变。

RSS 脚本默认测 ONNX 模型及一次合成识别；`--qt` 在同一进程加载 app/SMS/模型、完成 Qt 登录及泄露检查，再识别一次合成图。输出进程峰值和每 50ms 采样的含 Chromium 子进程 RSS 总和（共享页会重复计数，采样可能错过短峰，非 cgroup 内存值）。本地小页面的内存占用不能代表真实官方页面或部署服务峰值。没有真实登录、选择器校准或线上通过率结论。

2026-10-31 本地验证（Python 3.13、QtWebEngine 6.12.0、pywebview 6.2.1）：上述双标志全量 unittest **362 项通过、0 跳过**；两个 Node 脚本通过（SMS 12 项，report 47 条断言）。新增 6 项真实 Qt 短信回归，可信事件和泄露检查通过。独立 ONNX 识别进程峰值 **342.7 MiB**；app/SMS/模型加 Qt 完整流程进程峰值 **684.2 MiB**，含 Chromium 的采样进程树峰值 **897.8 MiB**，低于模板的 3072 MiB 硬上限。未部署、未发真实短信、未消耗取数额度。
