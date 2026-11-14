#!/bin/sh
# XD KVM clipboard agent - bo cai cho Linux desktop (khong can root).
# Chep agent vao ~/.local/share/xdclip, them autostart, chay ngay.
B='__BASE__'
D="$HOME/.local/share/xdclip"
A="$HOME/.config/autostart/xdclip.desktop"
command -v python3 >/dev/null || { echo "XD KVM clipboard agent: LOI - can python3"; exit 1; }
mkdir -p "$D" "$(dirname "$A")"
if [ -f "$D/pid" ] && grep -q agent.py "/proc/$(cat "$D/pid")/cmdline" 2>/dev/null; then kill "$(cat "$D/pid")"; sleep 0.5; fi
if command -v curl >/dev/null; then curl -fsS "$B__DL__/agent.py?b=$B" -o "$D/agent.py.new"
else wget -qO "$D/agent.py.new" "$B__DL__/agent.py?b=$B"; fi || { echo "XD KVM clipboard agent: LOI - khong tai duoc agent"; exit 1; }
mv "$D/agent.py.new" "$D/agent.py"
cat > "$A" <<EOF
[Desktop Entry]
Type=Application
Name=XD KVM Clipboard
Comment=Dong bo clipboard voi W105D KVM
Exec=python3 $D/agent.py
NoDisplay=true
X-GNOME-Autostart-enabled=true
EOF
nohup setsid python3 "$D/agent.py" >/dev/null 2>&1 &
if ! command -v xclip >/dev/null && ! command -v xsel >/dev/null && ! command -v wl-paste >/dev/null; then
  echo "XD KVM clipboard agent: da cai, nhung CAN cai them xclip (sudo apt install xclip)"
else
  echo "XD KVM clipboard agent: DA CAI XONG"
fi
