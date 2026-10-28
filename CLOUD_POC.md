# EC2 无浏览器单次取数 PoC

此 PoC 验证：在外部电脑完成一次官方扫码登录后，把本次会话直接交给 EC2，由 EC2 请求一个关键词、包含首尾的七天搜索指数。EC2 不安装或运行浏览器。外部电脑可以是 Windows、Linux 或 macOS，不要求使用 Mac。

这不是全云登录服务，也不是常驻采集服务。登录助手的 SSH 连接必须保持到云端查询结束；关闭登录浏览器不等于让源设备断网。跨 IP 会话能否使用、源设备断网后的行为、会话寿命及长期稳定性均待实际验证。原桌面版本的历史验证不能代替本 PoC 的验收。

## 组成与数据流

- `bootstrap_cloud.sh`：只安装项目内 Python 运行环境与核心依赖，不启动服务。
- `login_transfer.py`：仅在外部登录电脑运行。先执行无凭证的远端自检，再启动新的临时浏览器会话，由用户完成官方扫码及必要的验证码交互。
- `cloud_probe.py`：仅从有界标准输入接收本次会话，执行一次单词七天查询，返回筛选后的指数结果，并在 `finally` 中清除 Cookie、关闭 HTTP 会话及清理输入对象。

用户输入 `SEND` 后，助手从当前临时会话提取仅适用于固定取数端点的 Cookie，收窄为 `creator.douyin.com` 的 HTTPS host-only Cookie，关闭临时浏览器，再通过 SSH 标准输入发送给 EC2。关闭浏览器不是退出抖音账号。传输不经过 ChatGPT、GitHub、命令行参数、环境变量或 Cookie 文件。

云端不保存登录状态或原始业务响应，也不保留后台进程。结果经 SSH 返回登录助手，在该电脑终端显示日期与搜索指数；这条路径不使用桌面版 `results/` 持久化或缓存。Python 对象清理不等于对进程内存进行可证明的物理擦除。

## EC2 安装

目标目录为 `/srv/work-ecs-projects/douyin-index-local`，由普通项目账号 `work-aws-manager` 运行。保留现有项目目录的 `700` 权限，不为了普通 SSH 账号访问而放宽权限。

先把已审阅的项目源码部署到目标目录。该目录中的 `cloud_probe.py`、`client.py`、`requirements-core.txt` 和离线测试文件应来自同一版本。安装脚本针对 Linux x86_64，要求已有 `bash`、`curl`、`sha256sum`、`tar`、`timeout`，并可通过 HTTPS 访问官方 GitHub release 与 PyPI 下载站点。

以项目账号在目标目录逐步执行，前一步成功才继续：

```sh
bash bootstrap_cloud.sh uv
bash bootstrap_cloud.sh python
bash bootstrap_cloud.sh venv
bash bootstrap_cloud.sh deps
bash bootstrap_cloud.sh check
```

也可在足够长的前台命令时限内运行 `bash bootstrap_cloud.sh all`。工具若会在命令结束时清理进程组，使用上述分步命令；不要用 `nohup`、`&` 或启动后台服务。

脚本固定 uv `0.13.6`、CPython `3.12.16`、Python 构建 `20261026`。uv 从官方 GitHub release 下载，提取前检查固定 SHA-256：

```text
f29c3e94ffabd7e6f3ba521dba169a9b7ec12100bf3257087f0f24f6123f3645
```

不执行远程安装脚本，不使用 `curl | sh`，不调用 `sudo`，不更改系统 Python、shell 配置或目录授权。uv、managed Python、下载和缓存均在项目 `.runtime/`；虚拟环境在 `.venv/`。安装 uv 归档最多等待 90 秒；每个 uv 阶段限制 120 秒，网络不自动重试。核心依赖只接受 wheel，避免在小规格主机上编译。失败后先检查原因，再决定是否手动重跑对应安装阶段。

完成后运行离线测试；测试使用合成数据，不需要登录或请求抖音：

```sh
.venv/bin/python -m unittest -v \
  test_client.py test_storage.py test_app.py \
  test_cloud_probe.py test_login_transfer.py
.venv/bin/python -B cloud_probe.py --self-check
```

`--self-check` 只检查解释器、核心依赖和端点约定，不验证抖音连通性或登录。

### 2026-10-28 EC2 部署检查

已在目标 EC2 安装 uv `0.13.6`、Python `3.12.16` 与 `requirements-core.txt` 固定的核心依赖；88 项离线测试全部通过，`cloud_probe.py --self-check` 通过。这些检查没有携带真实会话，也没有执行真实抖音取数。

单次自检子进程最大 RSS 为 37496 KiB（约 36.6 MiB），只代表该自检进程，不代表真实请求、并发或整台服务器的资源需求。`.runtime` 当时含下载和缓存约 213 MiB；虚拟环境可能使用缓存硬链接，不能把各目录的独立统计简单相加当作实际磁盘占用。

外部电脑扫码、真实会话传输、跨 IP 取数、官方页面逐日比对及源设备断网测试均未完成。

## 外部登录电脑准备

准备 Python 3.12、系统 OpenSSH 客户端、同版本项目源码，以及已经能连接目标 EC2 且已核验主机密钥的 SSH 别名。助手启用严格主机密钥检查、非交互密钥认证，不转发 SSH agent，不自动接受新主机密钥。

macOS / Linux 示例：

```sh
python3.12 -m venv .venv-login
.venv-login/bin/python -m pip install -r requirements-login.txt
```

Windows PowerShell 示例：

```powershell
py -3.12 -m venv .venv-login
.\.venv-login\Scripts\python.exe -m pip install -r requirements-login.txt
```

`requirements-login.txt` 包含核心依赖与 Playwright `1.64.1`。默认使用此电脑已有的 Chrome；使用 Edge 时传 `--channel msedge`。助手启动单独的浏览器进程与临时 context，不读取日常 Chrome / Edge 用户资料。

若使用 Playwright Chromium，只在这台外部电脑安装浏览器：

```sh
# macOS / Linux；Windows 改用 .\.venv-login\Scripts\python.exe
.venv-login/bin/python -m playwright install chromium
```

随后传 `--channel chromium`。Linux 登录电脑还需要可用图形桌面及浏览器系统依赖；依赖安装由该电脑的现有管理员处理。EC2 不执行这些浏览器安装步骤。

## 单次运行与人工核对

以下命令中的 `2026-10-25` 是桌面版本曾验证过的**历史截止日示例，不是最新可用日**。实际运行必须使用官方页面显示的可用截止日，不能默认昨天可取。对应开始日由工具计算为截止日前六天。

在已激活的登录虚拟环境中执行；未激活时将 `python` 换成上面的虚拟环境解释器完整路径：

```sh
python login_transfer.py \
  --ssh-host YOUR_EC2_ALIAS \
  --run-as work-aws-manager \
  --keyword 酵素 \
  --end-date 2026-10-25
```

Windows PowerShell 可写为一行：

```powershell
.\.venv-login\Scripts\python.exe login_transfer.py --ssh-host YOUR_EC2_ALIAS --run-as work-aws-manager --keyword 酵素 --end-date 2026-10-25
```

`YOUR_EC2_ALIAS` 替换为该电脑已有的 SSH 别名。普通 SSH 管理账号使用 `--run-as work-aws-manager`，助手仅调用已获授权的 `sudo -n -u work-aws-manager`；不会创建 sudo 规则或更改服务器权限。如果 SSH 本来就以项目账号连接，可以省略此参数。无现成免密切换权限时，自检失败即停止，不让助手索取管理员密码。

1. 先等远端自检通过。失败时不会启动登录浏览器。
2. 在新打开的官方页面用手机抖音扫码。验证码或其他账户确认由用户在官方页面完成，不使用绕过方式。
3. 进入抖音指数，确认一个关键词、全国、抖音和指定的连续七天。若页面日期与命令不一致，取消本次操作并修正参数；未收录的词也取消，不把未收录当作零。
4. 在发送会话前，独立记录折线图七天的完整「关键词搜索指数」提示值及日期。不要从程序结果复制回填，不用带「万」等缩写代替精确值。
5. 确认目标主机和查询范围后输入 `SEND`。其他输入取消，不向 EC2 发送会话。保持登录助手和 SSH 在线直至退出；临时浏览器会在请求前关闭。
6. 将 EC2 返回的七个日期和搜索指数逐一与先前记录比较。只有日期齐全、指标口径正确且七天精确相等，才记录此单次查询通过。搜索指数不是实际搜索次数，综合指数也不是搜索指数。

登录错误、验证码、拒绝访问、限频、网络错误或响应格式变化均停止，不自动重试。SSH 超时或断开时，查询可能已经发出；先核查，不立即重跑。缺失保留为缺失，不能补零或宣称完整。未完成逐日页面核对时，成功响应仍不等于验证通过。

## 验收记录（待执行者填写）

仅登记不含凭证的结论。真实 Cookie、浏览器状态、SSH 私钥、原始认证报文不得粘贴到聊天、工单、GitHub 或工具调用参数；不要开启浏览器协议调试、HAR、trace、截图归档或终端会话录制来收集登录材料。原始页面观察和实际指数值按项目规则留在获授权的本地记录中，不随源码提交。

| 检查项 | 结果 |
|---|---|
| EC2 部署的源码版本 | 待填写 |
| Python / 核心依赖及离线测试 | 2026-10-28：Python 3.12.16、固定核心依赖，88 项离线测试通过 |
| 登录电脑操作系统与浏览器 | 待填写 |
| 远端无凭证自检 | 2026-10-28：EC2 直接执行通过；外部助手经 SSH 自检待验证 |
| 外部登录、SSH 单次传输与 EC2 请求 | 待填写 |
| 一个词、七个日期及官方页面逐日精确核对 | 待填写 |
| 会话与进程清理检查 | 待填写 |
| 失败类别或仍未解决的问题 | 待填写 |

此表不代表已测试跨 IP 会话、源设备断网、重启恢复或长期运行。PoC 不持久化会话，不提供自动续期、后台调度或云端扫码入口。是否继续建设这些能力，需在单次验证结果明确后另行设计。

## 参考

- [uv 官方 release 0.13.6](https://github.com/astral-sh/uv/releases/tag/0.13.6) 与 [SHA-256 清单](https://github.com/astral-sh/uv/releases/download/0.13.6/sha256.sum)。
- [uv managed Python](https://docs.astral.sh/uv/guides/install-python/) 与 [运行目录配置](https://docs.astral.sh/uv/reference/environment/)。
- [Python standalone 的 Linux 运行要求](https://docs.astral.sh/python-build-standalone/running/)。
- [Playwright 浏览器安装](https://playwright.dev/python/docs/browsers) 与 [临时 BrowserContext](https://playwright.dev/python/docs/api/class-browsercontext)。这些通用能力不保证抖音接受跨设备、跨 IP 会话。
