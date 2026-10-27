# 抖音搜索指数本地小批量验证工具

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
- **纯云端 Work：** 可进行源码优化和离线测试；当前 macOS 窗口和抖音登录会话不会自动传到云端。现有应用没有无头扫码或云端真实取数入口，不能把离线测试通过当作线上取数通过。
- **Work Cloud 的本地电脑访问：** 如果账号/工作区已启用相应能力，且 Mac 在线、已连接，可以让需要本地资源的步骤在 Mac 上执行；不要仅凭任务名里有“Cloud”推断它实际使用哪台机器。

无桌面环境只安装核心依赖即可跑全部离线测试，测试不会访问真实抖音接口：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-core.txt
.venv/bin/python -m unittest -v test_client.py test_storage.py test_app.py
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
