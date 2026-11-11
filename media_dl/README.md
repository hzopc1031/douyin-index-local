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

登录默认无头运行，二维码保存在 `/workspace/dev/media-dl-run/xhs-qr.png`，二维码优先由页面接口地址生成并自动刷新；生成和预计过期时间保存在 `xhs-qr.meta`，事件保存在 `xhs-login.log`。状态保存在 `/workspace/dev/media-dl-run/xhs-login.status`（`need_scan`、`scanned`、`logged_in`、`expired` 或 `error`）。登录资料保存在 `~/.config/media-dl/xhs-profile`。小红书流程尚未进行真实网络验证。

整页截图保存在 `/workspace/dev/media-dl-run/xhs-page.png`，可用于核对登录弹窗；截图可能包含敏感信息，请妥善保管。

可选转写：`python -m pip install -r media_dl/requirements-asr.txt`，加 `--asr` 后使用 faster-whisper；模型默认为 base，也可选择 small，首次使用由依赖下载到用户缓存。转写失败仍保留视频，元数据记录错误。未安装依赖时也保留视频。

离线测试：

```sh
python -m unittest discover -s media_dl/tests -v
```
