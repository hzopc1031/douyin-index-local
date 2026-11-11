# 批量视频下载

Python 3 运行；核心下载只用标准库。每行一个链接，空行及以 `#` 开头的行忽略：

```sh
python -m media_dl --input urls.txt --out ./out
python -m media_dl URL [URL ...] --out ./out --asr
python -m media_dl --input urls.txt --out ./out --asr --asr-model base
```

不指定 `--out` 时写入 `./media-dl-out`。串行处理，间隔 3 秒；每条写入元数据，失败不影响后续条目，批次有失败时退出码非零。视频和元数据保存在输出目录。请勿提交输出或含敏感信息的链接文件。

小红书先安装 Playwright 和浏览器，再扫码登录：

```sh
pip install -r media_dl/requirements-login.txt && python -m playwright install chromium
python -m media_dl login xhs
python -m media_dl 小红书链接 --out ./out
```

登录和下载可分别使用 `--proxy 127.0.0.1:8791`，也可设置环境变量 `MEDIA_DL_PROXY`；命令行优先，省略协议时使用 HTTP。代理仅用于小红书。

登录使用有界面浏览器（无显示器时使用虚拟显示），二维码保存在 `/workspace/dev/media-dl-run/xhs-qr.png`。登录码优先使用与页面轮询一致的原生码，拿不到时使用接口生成的码；安全验证出现时使用弹层里的原生码，过期则在弹层内刷新。码的类型（`login` 或 `verify`）、生成时间和官方过期时间（未提供时为空）保存在 `xhs-qr.meta`，事件及状态码保存在 `xhs-login.log`。状态保存在 `/workspace/dev/media-dl-run/xhs-login.status`（`need_scan`、`scanned`、`verify`、`logged_in`、`expired` 或 `error`）。登录资料保存在 `~/.config/media-dl/xhs-profile`。安全验证处理尚未经过真实登录验证。

整页截图保存在 `/workspace/dev/media-dl-run/xhs-page.png`，可用于核对登录弹窗；截图可能包含敏感信息，请妥善保管。

可选转写：`python -m pip install -r media_dl/requirements-asr.txt`，加 `--asr` 后使用 faster-whisper；模型默认为 base，也可选择 small，首次使用由依赖下载到用户缓存。转写失败仍保留视频，元数据记录错误。未安装依赖时也保留视频。

离线测试：

```sh
python -m unittest discover -s media_dl/tests -v
```
