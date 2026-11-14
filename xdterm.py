#!/usr/bin/env python3
"""
xdterm.py - PTY session host cho W105D (API giong S-remote), dich vu xd-term.

Chay bang root, chi nghe 127.0.0.1:8092; kvmweb (:8090) kiem tra token roi proxy sang day.
Phien song doc lap voi kvmweb (restart kvmweb khong mat phien); restart xd-term thi mat het.

  GET    /api/sessions                      danh sach
  POST   /api/sessions                      {"name","cwd","user":"root|sang","cols","rows","by"} -> {"id",...}
  DELETE /api/sessions/<id>                 kill (va xoa khoi danh sach)
  POST   /api/sessions/<id>/input           {"line": "lenh"} (them Enter) | {"data": "phim tho"} -> {"offset"}
  GET    /api/sessions/<id>/output?since=N&wait=ms&tail=N
                                            log van ban (bo ANSI, 256 KB cuoi) -> {data,since,next,base,truncated,exited,code}
  POST   /api/sessions/<id>/resize          {"cols","rows"}
  POST   /api/sessions/<id>/rename          {"name"}
  WS     /api/sessions/<id>/ws              xem/go truc tiep: server gui text (output tho, co ANSI);
                                            client gui JSON {"t":"in","d":...} | {"t":"rs","c":..,"r":..}
"""
import os, sys, pty, fcntl, termios, struct, select, signal, json, threading, time, re, socket, base64, hashlib, pwd, codecs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8092
LOG_MAX = 256 * 1024          # log van ban cho agent
RAW_MAX = 256 * 1024          # output tho (co ANSI) de phat lai cho trang /terminal
MAX_SESS = 24
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[PX^_][^\x1b]*\x1b\\|\x1b[ -/]+[0-~]|\x1b[@-Z\\-_]")
ANSI_ONE = re.compile(r"^(?:" + ANSI_RE.pattern + ")")
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

def plain(t):
    t = ANSI_RE.sub("", t)
    t = re.sub(r"\r+\n", "\n", t)
    return CTRL_RE.sub("", t)

sessions, lock, seq = {}, threading.Lock(), [0]

class Sess:
    def __init__(self, name, cwd, user, cols, rows, by):
        with lock:
            seq[0] += 1
            self.id = f"t{int(time.time()):x}{seq[0]:02d}"
        self.name = (name or f"{user} {seq[0]}")[:60]
        self.user, self.by, self.created = user, by, time.time()
        self.exited, self.code, self.last = False, None, 0.0
        self.cols, self.rows = cols, rows
        self.raw = bytearray()
        self.log, self.base, self.pend = "", 0, ""
        self.cv = threading.Condition()
        self.clients = set()
        self.dec = codecs.getincrementaldecoder("utf-8")("replace")
        pw = pwd.getpwnam(user)
        home = pw.pw_dir
        cwd = cwd if cwd and os.path.isdir(cwd) else home
        self.cwd = cwd
        env = {"TERM": "xterm-256color", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HOME": home, "USER": user, "LOGNAME": user,
               "SHELL": pw.pw_shell or "/bin/bash", "PAGER": "cat", "GIT_PAGER": "cat", "SYSTEMD_PAGER": "cat",
               "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "XD_SESSION": self.id}
        shell = pw.pw_shell if pw.pw_shell and os.path.exists(pw.pw_shell) and not pw.pw_shell.endswith("nologin") else "/bin/bash"
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(cwd)
                if pw.pw_uid != os.getuid():
                    os.initgroups(user, pw.pw_gid); os.setgid(pw.pw_gid); os.setuid(pw.pw_uid)
                os.execve(shell, [shell, "-l"], env)
            finally:
                os._exit(127)
        self.pid, self.fd = pid, fd
        self.resize(cols, rows)
        threading.Thread(target=self.reader, daemon=True).start()

    def resize(self, cols, rows):
        self.cols, self.rows = max(20, min(500, int(cols))), max(5, min(300, int(rows)))
        try: fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", self.rows, self.cols, 0, 0))
        except OSError: pass

    def reader(self):
        while True:
            try:
                r, _, _ = select.select([self.fd], [], [], 1.0)
                if not r:
                    if self.exited: break
                    continue
                b = os.read(self.fd, 65536)
            except OSError: b = b""
            if not b: break
            self.feed(b)
        try: _, st = os.waitpid(self.pid, 0); self.code = os.waitstatus_to_exitcode(st)
        except ChildProcessError: pass
        try: os.close(self.fd)
        except OSError: pass
        with self.cv:
            self.exited = True; self.cv.notify_all()
        for c in list(self.clients): c.close_soon()

    def feed(self, b):
        t = self.dec.decode(b)
        with self.cv:
            self.last = time.time()
            self.raw += b
            if len(self.raw) > RAW_MAX: del self.raw[:len(self.raw) - RAW_MAX]
            d = self.pend + t; self.pend = ""
            k = d.rfind("\x1b")   # chuoi escape bi cat giua 2 lan doc -> giu lai
            if k >= 0 and len(d) - k < 256 and not ANSI_ONE.match(d[k:]): self.pend, d = d[k:], d[:k]
            p = plain(d)
            if p:
                self.log += p
                if len(self.log) > LOG_MAX:
                    cut = len(self.log) - LOG_MAX; self.log = self.log[cut:]; self.base += cut
            self.cv.notify_all()
            cl = list(self.clients)          # chup trong khoa: khop voi ban phat lai luc client ket noi
        for c in cl: c.out(t)

    def end(self): return self.base + len(self.log)

    def write(self, data):
        if self.exited: raise OSError("phiên đã thoát")
        b = data.encode()
        while b:
            n = os.write(self.fd, b); b = b[n:]

    def kill(self):
        for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
            if self.exited: return
            try: os.killpg(self.pid, sig)
            except OSError:
                try: os.kill(self.pid, sig)
                except OSError: return
            for _ in range(10):
                if self.exited: return
                time.sleep(0.1)

    def info(self):
        return {"id": self.id, "name": self.name, "user": self.user, "by": self.by, "cwd": self.cwd, "shell": "bash",
                "createdAt": int(self.created * 1000), "lastOut": int(self.last * 1000), "exited": self.exited,
                "code": self.code, "clients": len(self.clients), "cols": self.cols, "rows": self.rows}

# ---------------- WebSocket toi gian ----------------
class WS:
    def __init__(self, h):
        self.h, self.sock, self.wl, self.alive = h, h.connection, threading.Lock(), True
    def send(self, op, data):
        n = len(data)
        hdr = bytes([0x80 | op]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 65536 else bytes([127]) + struct.pack(">Q", n))
        with self.wl:
            self.sock.sendall(hdr + data)
    def out(self, t):
        if not self.alive: return
        try: self.send(1, t.encode())
        except OSError: self.alive = False
    def close_soon(self):
        try: self.send(8, b"")
        except OSError: pass
        self.alive = False
    def recv(self):
        rf = self.h.rfile
        def rd(n):
            b = rf.read(n)
            if len(b) < n: raise EOFError
            return b
        msg, op0 = b"", None
        while True:
            b0, b1 = rd(2)
            op, fin, n = b0 & 15, b0 & 0x80, b1 & 127
            if n == 126: n = struct.unpack(">H", rd(2))[0]
            elif n == 127: n = struct.unpack(">Q", rd(8))[0]
            if n > (1 << 20): raise EOFError
            mask = rd(4) if b1 & 0x80 else b"\0\0\0\0"
            d = bytearray(rd(n))
            for i in range(n): d[i] ^= mask[i & 3]
            if op == 8: raise EOFError
            if op == 9: self.send(10, bytes(d)); continue
            if op == 10: continue
            if op: op0 = op
            msg += d
            if fin: return op0, msg

def find(sid):
    with lock: return sessions.get(sid)

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _json(self, d, code=200):
        b = json.dumps(d, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(b)
    def _err(self, code, msg): self._json({"error": msg}, code)
    def _body(self):
        try: n = int(self.headers.get("Content-Length", 0))
        except ValueError: n = 0
        raw = self.rfile.read(min(n, 1 << 20)) if n else b""
        try: return json.loads(raw or b"{}")
        except ValueError: return None

    def do_GET(self):
        path, _, q = self.path.partition("?")
        qs = dict(p.split("=", 1) if "=" in p else (p, "") for p in q.split("&") if p)
        if path == "/api/sessions":
            with lock: l = [s.info() for s in sessions.values()]
            return self._json(sorted(l, key=lambda s: s["createdAt"]))
        m = re.fullmatch(r"/api/sessions/([\w-]+)/(output|ws)", path)
        if not m: return self._err(404, "not found")
        s = find(m.group(1))
        if not s: return self._err(404, "không có phiên này")
        if m.group(2) == "ws": return self.ws(s)
        try: wait = max(0, min(60000, int(qs.get("wait", 0) or 0)))
        except ValueError: wait = 0
        since = int(qs["since"]) if qs.get("since", "").lstrip("-").isdigit() else None
        tail = int(qs["tail"]) if qs.get("tail", "").isdigit() else None
        with s.cv:
            if wait and since is not None and since >= s.end() and not s.exited:
                s.cv.wait_for(lambda: s.end() > since or s.exited, wait / 1000)
            end = s.end()
            sn = s.base if since is None else since
            if tail is not None: sn = max(sn, end - tail)
            fr = max(s.base, min(sn, end))
            r = {"data": s.log[fr - s.base:], "since": fr, "next": end, "base": s.base, "truncated": sn < s.base,
                 "exited": s.exited, "code": s.code}
        self._json(r)

    def do_POST(self):
        path = self.path.split("?")[0]
        d = self._body()
        if not isinstance(d, dict): return self._err(400, "bad json")
        if path == "/api/sessions":
            with lock:
                live = sum(1 for s in sessions.values() if not s.exited)
            if live >= MAX_SESS: return self._err(429, f"tối đa {MAX_SESS} phiên đang chạy")
            user = str(d.get("user") or "root")
            try: pwd.getpwnam(user)
            except KeyError: return self._err(400, f"không có user {user}")
            try:
                s = Sess(str(d.get("name") or "").strip(), str(d.get("cwd") or ""), user,
                         int(d.get("cols") or 160), int(d.get("rows") or 45), str(d.get("by") or "api")[:16])
            except (OSError, ValueError) as e: return self._err(500, str(e))
            with lock: sessions[s.id] = s
            return self._json(s.info())
        m = re.fullmatch(r"/api/sessions/([\w-]+)/(input|resize|rename)", path)
        if not m: return self._err(404, "not found")
        s = find(m.group(1))
        if not s: return self._err(404, "không có phiên này")
        op = m.group(2)
        if op == "input":
            data = d["line"] + "\r" if isinstance(d.get("line"), str) else d.get("data") if isinstance(d.get("data"), str) else ""
            if not data: return self._err(400, "cần line hoặc data")
            if s.exited: return self._err(409, "phiên đã thoát")
            with s.cv: off = s.end()
            try: s.write(data)
            except OSError as e: return self._err(409, str(e))
            return self._json({"offset": off})
        if op == "resize":
            s.resize(d.get("cols", s.cols), d.get("rows", s.rows)); return self._json(s.info())
        n = str(d.get("name", "")).strip()[:60]
        if n: s.name = n
        return self._json(s.info())

    def do_DELETE(self):
        m = re.fullmatch(r"/api/sessions/([\w-]+)", self.path.split("?")[0])
        s = find(m.group(1)) if m else None
        if not s: return self._err(404, "không có phiên này")
        threading.Thread(target=s.kill, daemon=True).start()
        with lock: sessions.pop(s.id, None)
        self._json({"ok": True})

    def ws(self, s):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or "websocket" not in self.headers.get("Upgrade", "").lower(): return self._err(400, "cần websocket")
        acc = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101); self.send_header("Upgrade", "websocket"); self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", acc); self.end_headers()
        self.close_connection = True
        w = WS(self)
        with s.cv:
            replay = bytes(s.raw).decode("utf-8", "replace")
            s.clients.add(w)
        try:
            w.send(1, json.dumps({"t": "hello", "info": s.info()}).encode())
            if replay: w.send(1, replay.encode())
            if s.exited: w.send(1, json.dumps({"t": "exit", "code": s.code}).encode())
            while w.alive:
                op, msg = w.recv()
                if op != 1: continue
                try: m = json.loads(msg)
                except ValueError: continue
                if not isinstance(m, dict): continue
                if m.get("t") == "in" and isinstance(m.get("d"), str) and not s.exited:
                    try: s.write(m["d"])
                    except OSError: pass
                elif m.get("t") == "rs":
                    try: s.resize(m.get("c", s.cols), m.get("r", s.rows))
                    except (TypeError, ValueError): pass
        except (OSError, EOFError, ValueError): pass
        finally:
            w.alive = False
            with s.cv: s.clients.discard(w)

class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

def main():
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    host = "127.0.0.1"
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    print(f"xd-term: http://{host}:{port}", flush=True)
    Server((host, port), H).serve_forever()

if __name__ == "__main__":
    main()
