#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"
if [[ ! -x ./.venv/bin/python ]]; then
  print "缺少 Python 环境。请先按 docs/contact-ui.md 的首次安装步骤安装。"
  exit 1
fi
if [[ ! -f assistant.local.json ]]; then
  cp -n assistant.user.example.json assistant.local.json
fi
mkdir -p logs
print "本地联系人管理：http://127.0.0.1:8765；页面右上角可退出程序，也可在此按 Control+C。"
print "此页面不会启动微信自动回复，也不会发送真实消息。"
exec ./.venv/bin/python -m src.core.assistant.web --config assistant.local.json --db logs/assistant.sqlite3
