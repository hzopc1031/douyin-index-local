# 批量视频下载

Python 3 运行；核心下载只用标准库。每行一个链接，空行及以 `#` 开头的行忽略：

```sh
python -m media_dl --input urls.txt --out ./out
python -m media_dl URL [URL ...] --out ./out --asr
python -m media_dl --input urls.txt --out ./out --asr --asr-model base
```

不指定 `--out` 时写入 `./media-dl-out`。串行处理，间隔 3 秒；每条写入元数据，失败不影响后续条目，批次有失败时退出码非零。视频和元数据保存在输出目录。请勿提交输出或含敏感信息的链接文件。

小红书需要自行登录后通过 `XHS_COOKIE` 或指向本地文件的 `XHS_COOKIE_FILE` 提供 Cookie。小红书支持为预留实现，尚未进行真实网络验证。

可选转写：`python -m pip install -r media_dl/requirements-asr.txt`，加 `--asr` 后使用 faster-whisper；模型默认为 base，也可选择 small，首次使用由依赖下载到用户缓存。转写失败仍保留视频，元数据记录错误。未安装依赖时也保留视频。

离线测试：

```sh
python -m unittest discover -s media_dl/tests -v
```
