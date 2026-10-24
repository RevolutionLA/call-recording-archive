#!/usr/bin/env bash
# 停止驾驶舱（对位 Windows 的「停止 CallRec.bat」）
# 正在跑的处理阶段按通话粒度保存进度，下次继续即可。
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f "logs/web.pid" ]; then
  echo "没有找到 logs/web.pid，可能没在用 scripts/start.sh 启动过"
  exit 0
fi

pid=$(cat logs/web.pid)
if kill -0 "$pid" 2>/dev/null; then
  kill "$pid"
  for _ in $(seq 1 20); do
    sleep 0.5
    kill -0 "$pid" 2>/dev/null || break
  done
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid" || true
  echo "已停止（pid $pid）"
else
  echo "进程 $pid 已不在运行"
fi
rm -f logs/web.pid
