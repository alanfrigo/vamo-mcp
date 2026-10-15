#!/bin/sh
set -eu
export DAEMON_PORT="${DAEMON_PORT:-9999}" NOVNC_PORT="${NOVNC_PORT:-6080}" VNC_PORT="${VNC_PORT:-5900}"
export DISPLAY=:99 XAUTHORITY=/home/agent/.Xauthority HOME=/home/agent LANG=C.UTF-8
python3 - <<'PY'
import errno, os, re, socket, uuid
ports = [os.environ[k] for k in ('DAEMON_PORT','NOVNC_PORT','VNC_PORT')]
if any(not re.fullmatch(r'[0-9]+', p) or not 1 <= int(p) <= 65535 for p in ports) or len(set(map(int, ports))) != 3:
    raise SystemExit('DAEMON_PORT, NOVNC_PORT and VNC_PORT must be distinct ports in 1..65535')
if not os.environ.get('SANDBOX_TOKEN'):
    raise SystemExit('SANDBOX_TOKEN is required')
try:
    uuid.UUID(os.environ.get('SANDBOX_SESSION_ID', ''))
except ValueError:
    raise SystemExit('SANDBOX_SESSION_ID must be a UUID')
if not re.fullmatch(r'[A-Za-z0-9]{8}', os.environ.get('VNC_PASSWORD','')):
    raise SystemExit('VNC_PASSWORD must contain exactly 8 ASCII alphanumeric characters')
with socket.socket(socket.AF_UNIX) as s:
    try:
        s.connect('\0/tmp/.X11-unix/X99')
    except OSError as e:
        if e.errno not in (errno.ENOENT, errno.ECONNREFUSED):
            raise SystemExit('X11 display :99 conflicts with a host abstract socket')
    else:
        raise SystemExit('X11 display :99 conflicts with a host abstract socket')
PY
install -d -m 1777 -o root -g root /tmp/.X11-unix
install -d -m 0700 -o agent -g agent /home/agent/.vnc
install -m 0600 -o agent -g agent /dev/null "$XAUTHORITY"
COOKIE="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
xauth -f "$XAUTHORITY" add :99 . "$COOKIE"
unset COOKIE
x11vnc -storepasswd "$VNC_PASSWORD" /home/agent/.vnc/passwd >/dev/null 2>&1
chown agent:agent "$XAUTHORITY" /home/agent/.vnc/passwd
chmod 0600 "$XAUTHORITY" /home/agent/.vnc/passwd
exec supervisord -n -c /etc/supervisor/supervisord.conf
