#!/bin/zsh
set -eu
cd -- "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
    print -u2 '请先创建虚拟环境：python3 -m venv .venv'
    print -u2 '然后安装依赖：.venv/bin/python -m pip install -r requirements.txt pyinstaller==6.22.3'
    exit 1
fi
if ! .venv/bin/python -c 'import PyInstaller' >/dev/null 2>&1; then
    print -u2 '请先安装打包依赖：.venv/bin/python -m pip install -r requirements.txt pyinstaller==6.22.3'
    exit 1
fi
trust_file="$(.venv/bin/python -c 'import ssl; from pathlib import Path; print(Path(ssl.__file__).with_name("codex-rejected-cas.pem"))')"
extra_data=()
if [[ -f "$trust_file" ]]; then
    extra_data+=(--add-data "${trust_file}:.")
fi
.venv/bin/python -m PyInstaller --noconfirm --windowed --name DouyinIndexLocal --osx-bundle-identifier local.codex.douyin-index --collect-all webview "${extra_data[@]}" launcher.py
