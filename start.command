#!/bin/zsh
set -eu
cd -- "${0:A:h}"
if [[ -x dist/DouyinIndexLocal.app/Contents/MacOS/DouyinIndexLocal ]]; then
    exec dist/DouyinIndexLocal.app/Contents/MacOS/DouyinIndexLocal
fi
if [[ ! -x .venv/bin/python ]]; then
    if ! command -v python3.12 >/dev/null 2>&1; then
        print -u2 '未找到 python3.12；请安装 Python 3.12 并将其加入 PATH，然后重试。'
        exit 1
    fi
    print '首次启动：正在创建 Python 3.12 虚拟环境。'
    python3.12 -m venv .venv
fi
if ! .venv/bin/python -c 'import requests, cryptography, webview' >/dev/null 2>&1; then
    print '正在安装项目依赖，需要连接网络。'
    if ! .venv/bin/python -m pip install -r requirements.txt; then
        print -u2 '依赖安装失败，请检查网络后重试。'
        exit 1
    fi
fi
exec .venv/bin/python app.py
