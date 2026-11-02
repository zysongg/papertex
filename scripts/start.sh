#!/bin/zsh
set -e
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
plist="$HOME/Library/LaunchAgents/$label.plist"
uid=$(id -u)
pidfile=logs/papertex.pid

if launchctl print "gui/$uid/$label" >/dev/null 2>&1; then
  launchctl kickstart -k "gui/$uid/$label"
  echo "已通过 launchd 重启 ($label)"
  exit 0
fi
if [ -f "$plist" ]; then
  launchctl bootstrap "gui/$uid" "$plist"
  echo "已通过 launchd 启动 ($label)"
  exit 0
fi

port=$(.venv/bin/python -c "import json;print(json.load(open('config/config.json'))['port'])")
if lsof -nP -iTCP:$port -sTCP:LISTEN >/dev/null 2>&1; then
  echo "端口 $port 已被占用,先执行 stop.sh 或手动检查: lsof -nP -iTCP:$port -sTCP:LISTEN"
  exit 1
fi
nohup .venv/bin/python app/server.py >> logs/papertex.log 2>&1 &
echo $! > "$pidfile"
echo "已启动 (pid $!) → http://127.0.0.1:$port"
