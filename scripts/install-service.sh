#!/bin/zsh
set -e
cd "$(dirname "$0")/.."
root=$PWD
label=${PAPERTEX_LABEL:-com.papertex.app}
python="$root/.venv/bin/python"
plist="$HOME/Library/LaunchAgents/$label.plist"
uid=$(id -u)

if [[ ! -x $python ]]; then
  echo "未找到 $python，请先按 README 创建 .venv" >&2
  exit 1
fi

mkdir -p "$root/logs" "$HOME/Library/LaunchAgents"

xml_escape() {
  print -r -- "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'
}

cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "$python")</string>
    <string>$(xml_escape "$root/app/server.py")</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$(xml_escape "$root")</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ProcessType</key>
  <string>Background</string>
  <key>StandardOutPath</key>
  <string>$(xml_escape "$root/logs/papertex.log")</string>
  <key>StandardErrorPath</key>
  <string>$(xml_escape "$root/logs/papertex.log")</string>
</dict>
</plist>
EOF
chmod 644 "$plist"

launchctl bootout "gui/$uid/$label" 2>/dev/null || true
launchctl bootstrap "gui/$uid" "$plist"
echo "已安装并启动 launchd 服务 ($label)"
echo "配置: $plist"
echo "日志: $root/logs/papertex.log"
echo "浏览器打开 http://127.0.0.1:8817（端口以 config/config.json 为准）"
