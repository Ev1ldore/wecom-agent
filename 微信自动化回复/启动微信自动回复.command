#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"
mkdir -p logs
export DB_URL="sqlite:///./logs/wechat-native.db"
export POLL_INTERVAL_SECONDS=5
echo "仅监听微信 2 的 Ri 独立单聊；跳过启动前历史。停止请在此终端按 Control+C。"
exec ./.venv/bin/python -u agent_loop.py --wechat-native --live
