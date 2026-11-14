#!/bin/zsh
set -eu
cd -- "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
    print -u2 '请先运行 start.command 创建项目环境，再运行此脚本。'
    exit 1
fi
print '正在安装评论采集依赖，需要联网并下载 Chromium。'
.venv/bin/python -m pip install -r tools/douyin_comments/requirements.txt
.venv/bin/python -m playwright install chromium
print '评论依赖安装完成。请重新打开本地窗口。'
