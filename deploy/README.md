# 拆分服务模板

systemd/ 下两个 unit 仅作模板，不自动安装；desktop 启动桌面和应用，gateway 仅启动 HTTPS 网关，重启网关不影响桌面。
模板使用 work-tencent-manager 账号和 /srv/work-ecs-projects/douyin-index-local 路径，可按部署环境调整。
INDEX_HUB_STATE_DIR 默认项目下 .cloud-state；两服务须配置同一目录以共享 socket、SQLite、access.json 和证书。
INDEX_HUB_LOG_DIR 默认项目下 logs，模板显式设置该路径；进程输出追加到 *.stdout.log。
INDEX_HUB_BIND 默认 0.0.0.0，INDEX_HUB_PORT 默认 8443（1–65535）。
启动桌面前，以服务账号运行 x11vnc -storepasswd，将口令保存到状态目录的 vnc.passwd 并 chmod 600；缺失或权限过宽会拒绝启动。
