#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"
mkdir -p logs
export DB_URL="sqlite:///./logs/wechat-native.db"
echo "仅监听微信 2 的 Ri 独立单聊；默认每60秒轮询，跳过启动前历史；异常停止，不自动重试。停止请在此终端按 Control+C。"
exec ./.venv/bin/python -u agent_loop.py --wechat-native --live
