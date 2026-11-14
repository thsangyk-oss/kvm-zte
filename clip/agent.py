#!/usr/bin/env python3
# XD KVM clipboard agent cho Linux desktop (W105D kvmweb phat file nay, __BASES__ duoc thay khi tai).
# Dong bo clipboard van ban 2 chieu voi W105D qua HTTP: POST /clip/agent moi ~0,6 s.
# Can xclip hoac xsel (X11/XWayland), hoac wl-clipboard (Wayland thuan).
import json, os, shutil, socket, subprocess, sys, time, urllib.request, fcntl, getpass

BASES = [__BASES__]
DIR = os.path.expanduser("~/.local/share/xdclip")
AUTOSTART = os.path.expanduser("~/.config/autostart/xdclip.desktop")
MAX = 500000

def tool():
    if os.environ.get("DISPLAY"):
        if shutil.which("xclip"): return (["xclip", "-o", "-selection", "clipboard", "-t", "UTF8_STRING"],
                                          ["xclip", "-i", "-selection", "clipboard"])
        if shutil.which("xsel"): return (["xsel", "-o", "-b"], ["xsel", "-i", "-b"])
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        return (["wl-paste", "-n", "-t", "text"], ["wl-copy"])
    return None

def get(t):
    try:
        r = subprocess.run(t[0], capture_output=True, timeout=2)
        return r.stdout.decode("utf-8", "replace")[:MAX] if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError): return None

def put(t, s):
    p = subprocess.Popen(t[1], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    p.stdin.write(s.encode()); p.stdin.close()
    try: p.wait(0.5)
    except subprocess.TimeoutExpired: pass   # xclip/wl-copy o lai giu selection

def main():
    os.makedirs(DIR, exist_ok=True)
    lk = open(os.path.join(DIR, "lock"), "w")
    try: fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError: sys.exit(0)                       # da co 1 agent dang chay
    with open(os.path.join(DIR, "pid"), "w") as f: f.write(str(os.getpid()))
    host, user = socket.gethostname(), getpass.getuser()
    bi, seq, last = 0, -1, None
    while True:
        t = tool()
        err = "" if t else "thiếu xclip/xsel/wl-clipboard"
        cur = get(t) if t else None
        body = {"id": host, "host": host, "user": user, "os": "linux", "v": 1, "seq": seq, "err": err}
        send = cur is not None and cur != last
        if send: body["text"] = cur
        try:
            rq = urllib.request.Request(BASES[bi] + "/clip/agent", json.dumps(body).encode(),
                                        {"Content-Type": "application/json; charset=utf-8", "X-Clip-Key": "__KEY__"})
            with urllib.request.urlopen(rq, timeout=10) as r: d = json.loads(r.read())
            if send: last = cur
            if d.get("uninstall"):
                for p in (AUTOSTART,):
                    try: os.unlink(p)
                    except OSError: pass
                shutil.rmtree(DIR, ignore_errors=True)
                return
            if d.get("push") is not None and d.get("seq") != seq and t:
                if d["push"]: put(t, d["push"]); last = d["push"]
            seq = d.get("seq", seq)
        except (OSError, ValueError):
            bi = (bi + 1) % len(BASES); time.sleep(2)
        time.sleep(0.6)

if __name__ == "__main__":
    main()
