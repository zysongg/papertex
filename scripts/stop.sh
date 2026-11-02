#!/bin/zsh
cd "$(dirname "$0")/.."
label=${PAPERTEX_LABEL:-}
if [[ -z $label ]]; then
  for plist in "$HOME"/Library/LaunchAgents/*.plist(N); do
    if grep -qF "$PWD/app/server.py" "$plist" 2>/dev/null; then
      label=${plist:t:r}
      break
    fi
  done
fi
label=${label:-com.papertex.app}
uid=$(id -u)
pidfile=logs/papertex.pid

if launchctl print "gui/$uid/$label" >/dev/null 2>&1; then
  launchctl bootout "gui/$uid/$label"
  rm -f "$pidfile"
  echo "已停止 launchd 服务 ($label)"
  exit 0
fi

if [ -f "$pidfile" ]; then
  pid=$(cat "$pidfile")
  if kill "$pid" 2>/dev/null; then
    for i in {1..25}; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.2
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null
      echo "已强制停止"
    else
      echo "已停止"
    fi
  else
    echo "进程不存在"
  fi
  rm -f "$pidfile"
else
  echo "没有 pid 文件,若服务在跑请手动查找: lsof -nP -iTCP:8817 -sTCP:LISTEN"
fi
