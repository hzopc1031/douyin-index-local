#!/bin/zsh
set -eu
cd -- "${0:A:h}"
if [[ -x dist/DouyinIndexLocal.app/Contents/MacOS/DouyinIndexLocal ]]; then
    exec dist/DouyinIndexLocal.app/Contents/MacOS/DouyinIndexLocal
fi
if [[ -x .venv/bin/python ]]; then
    exec .venv/bin/python app.py
fi
print -u2 '请先创建虚拟环境：python3 -m venv .venv'
print -u2 '然后安装依赖：.venv/bin/python -m pip install -r requirements.txt'
print -u2 '安装完成后再次运行 start.command。'
exit 1
