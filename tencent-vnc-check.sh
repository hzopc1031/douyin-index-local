#!/bin/bash
set -euo pipefail
passwd_file="$1/vnc.passwd"
mode=$(stat -c %a -- "$passwd_file" 2>/dev/null) || mode=""
if [[ ! -f "$passwd_file" || ! "$mode" =~ ^[0-7]{1,4}$ ]] || (( (8#${mode:-777} & ~8#600) != 0 )); then
  echo '缺少 VNC 口令文件，请以服务账号运行 x11vnc -storepasswd 生成' >&2
  exit 1
fi
