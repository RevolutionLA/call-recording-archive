#!/usr/bin/env bash
# Linux / macOS / NAS 启动脚本（对位 Windows 的「双击启动 CallRec.bat」）
#
# 用法：
#   scripts/start.sh              # 后台起驾驶舱，打印地址
#   scripts/start.sh --foreground # 前台跑（看得到日志，Ctrl-C 结束）
#   CALLREC_PORT=9000 scripts/start.sh
#
# 停止：scripts/stop.sh
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p logs data

PY="${PYTHON:-python3}"
if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"; fi

if [ ! -f "config.yaml" ]; then
  echo "⚠ 未找到 config.yaml，先复制模板：cp config.example.yaml config.yaml"
  exit 1
fi

if [ "${1:-}" = "--foreground" ]; then
  exec "$PY" pipeline.py web
fi

if [ -f "logs/web.pid" ] && kill -0 "$(cat logs/web.pid)" 2>/dev/null; then
  echo "已在运行（pid $(cat logs/web.pid)），直接打开 http://$(hostname -I 2>/dev/null | awk '{print $1}'):${CALLREC_PORT:-8760}"
  exit 0
fi

nohup "$PY" pipeline.py web > logs/web.log 2>&1 &
echo $! > logs/web.pid
sleep 2

if kill -0 "$(cat logs/web.pid)" 2>/dev/null; then
  echo "✅ 驾驶舱已启动（pid $(cat logs/web.pid)）"
  echo "   本机： http://127.0.0.1:${CALLREC_PORT:-8760}"
  echo "   局域网/NAS： http://$(hostname -I 2>/dev/null | awk '{print $1}'):${CALLREC_PORT:-8760}"
  echo "   日志： logs/web.log    停止： scripts/stop.sh"
else
  echo "❌ 启动失败，最后 20 行日志："
  tail -20 logs/web.log
  exit 1
fi
