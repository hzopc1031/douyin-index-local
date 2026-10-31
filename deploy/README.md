# 拆分服务模板

systemd/ 下两个 unit 仅作模板，不自动安装；desktop 启动桌面和应用，gateway 仅启动 HTTPS 网关，重启网关不影响桌面。
模板按完整方案写：服务账号 douyin-svc，状态 /var/lib/douyin-index，日志 /var/log/douyin-index；代码目录和 .runtime 只读，desktop 只额外可写 .cloud-home 和 results。不要设置 SuccessExitStatus=143（脚本在子进程被 SIGTERM 时以 143 退出，需要靠它触发 Restart=on-failure）。
INDEX_HUB_STATE_DIR 默认项目下 .cloud-state；两服务须配置同一目录以共享 socket、SQLite、access.json 和证书。
INDEX_HUB_LOG_DIR 默认项目下 logs；Python 日志只写轮转文件（INDEX_HUB_LOG_STDERR=1 才复制到 stderr），进程输出追加到 *.stdout.log，由 deploy/logrotate/douyin-index 轮转。
INDEX_HUB_BIND 默认 0.0.0.0，INDEX_HUB_PORT 默认 8443（1–65535）。
启动桌面前，以服务账号运行 x11vnc -storepasswd，将口令保存到状态目录的 vnc.passwd 并 chmod 600；缺失或权限过宽会拒绝启动。
