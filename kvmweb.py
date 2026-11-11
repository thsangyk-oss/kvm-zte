#!/usr/bin/env python3
"""
kvmweb.py - Web KVM cho ZTE W105D/W103D.

- GET /        giao dien dieu khien (video + ban phim/chuot)
- GET /ws      WebSocket: video JPEG (flow-control theo ack -> do tre thap) + input
- GET /stream  MJPEG stream (tuong thich cu)
- GET /snap    1 frame JPEG
- GET /health  trang thai JSON
- POST /input  JSON event (tuong thich cu)

Input duoc chuyen thanh lenh UDP toi hidfwd.py (127.0.0.1:5115).

Chay:  python3 kvmweb.py [--dev /dev/videoX] [--port 8090] [--fwd 127.0.0.1:5115]
"""
import os, sys, fcntl, mmap, struct, select, socket, json, threading, time, base64, hashlib, zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(HERE, "kvmweb.json")
DEV, PORT, FWD = "/dev/v4l/by-id/usb-MACROSILICON_2109-video-index0", 8090, ("127.0.0.1", 5115)
UDC_STATE = "/sys/class/udc/ff400000.usb/state"

# ---------------- V4L2 ----------------
def _ioc(d, t, n, s): return (d << 30) | (s << 16) | (t << 8) | n
V = ord('V')
VIDIOC_S_FMT        = _ioc(3, V, 5, 208)
VIDIOC_REQBUFS      = _ioc(3, V, 8, 20)
VIDIOC_QUERYBUF     = _ioc(3, V, 9, 88)
VIDIOC_QBUF         = _ioc(3, V, 15, 88)
VIDIOC_DQBUF        = _ioc(3, V, 17, 88)
VIDIOC_STREAMON     = _ioc(1, V, 18, 4)
VIDIOC_STREAMOFF    = _ioc(1, V, 19, 4)
VIDIOC_S_PARM       = _ioc(3, V, 22, 204)
VIDIOC_ENUM_FRAMESIZES     = _ioc(3, V, 74, 44)
VIDIOC_ENUM_FRAMEINTERVALS = _ioc(3, V, 75, 52)
BUF_CAPTURE, MEM_MMAP, FIELD_NONE = 1, 1, 1
MJPG = int.from_bytes(b"MJPG", "little")
NBUF = 3

def enum_modes(fd):
    """[(w,h,fps), ...] MJPEG, sap xep giam dan."""
    modes, i = [], 0
    while True:
        s = bytearray(44); struct.pack_into("II", s, 0, i, MJPG)
        try: fcntl.ioctl(fd, VIDIOC_ENUM_FRAMESIZES, s, True)
        except OSError: break
        typ = struct.unpack_from("I", s, 8)[0]
        if typ != 1: break  # chi ho tro DISCRETE
        w, h = struct.unpack_from("II", s, 12)
        k = 0
        while True:
            f = bytearray(52); struct.pack_into("IIII", f, 0, k, MJPG, w, h)
            try: fcntl.ioctl(fd, VIDIOC_ENUM_FRAMEINTERVALS, f, True)
            except OSError: break
            if struct.unpack_from("I", f, 16)[0] != 1: break
            n, d = struct.unpack_from("II", f, 20)
            if n: modes.append((w, h, round(d / n)))
            k += 1
        i += 1
    modes = sorted(set(modes), key=lambda m: (m[0] * m[1], m[2]), reverse=True)
    return modes

class Grabber(threading.Thread):
    """Doc MJPEG tu UVC, giu frame moi nhat; client tu lay frame moi nhat (khong xep hang)."""
    def __init__(self, dev, mode):
        super().__init__(daemon=True)
        self.dev, self.want = dev, tuple(mode)
        self.mode, self.modes = None, []
        self.latest, self.seq = None, 0
        self.cv = threading.Condition()
        self.err, self.fps, self.kbps, self.chg = "đang mở thiết bị", 0.0, 0, 0.0
        self._crc = None
        self._reset = False
        self.start()

    def set_mode(self, w, h, f):
        m = (int(w), int(h), int(f))
        if self.modes and m not in self.modes: return False
        self.want, self._reset = m, True
        try:
            with open(CONF, "w") as fp: json.dump({"mode": list(m)}, fp)
        except OSError: pass
        return True

    def wait_frame(self, last_seq, timeout=1.0):
        with self.cv:
            self.cv.wait_for(lambda: self.seq != last_seq, timeout)
            return self.seq, self.latest

    def run(self):
        while True:
            try: self._capture()
            except Exception as e:
                self.err, self.fps = str(e), 0.0
                time.sleep(1.5)

    def _capture(self):
        fd = os.open(self.dev, os.O_RDWR | os.O_NONBLOCK)
        bufs = []
        try:
            self._reset = False
            self.modes = enum_modes(fd)
            w, h, f = self.want
            if self.modes and (w, h, f) not in self.modes:
                w, h, f = next((m for m in self.modes if m[:2] == (w, h)), self.modes[0])

            fmt = bytearray(208)
            struct.pack_into("I", fmt, 0, BUF_CAPTURE)
            struct.pack_into("4I", fmt, 8, w, h, MJPG, FIELD_NONE)
            fcntl.ioctl(fd, VIDIOC_S_FMT, fmt, True)
            w, h = struct.unpack_from("2I", fmt, 8)

            parm = bytearray(204)
            struct.pack_into("I", parm, 0, BUF_CAPTURE)
            struct.pack_into("II", parm, 12, 1, f)
            try:
                fcntl.ioctl(fd, VIDIOC_S_PARM, parm, True)
                n, d = struct.unpack_from("II", parm, 12)
                if n: f = round(d / n)
            except OSError: pass
            self.mode = (w, h, f)

            req = bytearray(20)
            struct.pack_into("III", req, 0, NBUF, BUF_CAPTURE, MEM_MMAP)
            fcntl.ioctl(fd, VIDIOC_REQBUFS, req, True)
            for i in range(struct.unpack_from("I", req, 0)[0]):
                b = bytearray(88)
                struct.pack_into("II", b, 0, i, BUF_CAPTURE)
                struct.pack_into("I", b, 60, MEM_MMAP)
                fcntl.ioctl(fd, VIDIOC_QUERYBUF, b, True)
                off, ln = struct.unpack_from("I", b, 64)[0], struct.unpack_from("I", b, 72)[0]
                bufs.append(mmap.mmap(fd, ln, offset=off))
                fcntl.ioctl(fd, VIDIOC_QBUF, b, True)

            fcntl.ioctl(fd, VIDIOC_STREAMON, struct.pack("I", BUF_CAPTURE))
            print(f"capture {w}x{h}@{f}", flush=True)
            t0 = t_last = time.time(); cnt = nbytes = nchg = 0; t_pub = 0
            while not self._reset:
                r, _, _ = select.select([fd], [], [], 1.0)
                now = time.time()
                if not r:
                    if now - t_last > 5: raise RuntimeError("capture không trả frame (kiểm tra cáp HDMI/USB)")
                    continue
                b = bytearray(88)
                struct.pack_into("I", b, 4, BUF_CAPTURE)
                struct.pack_into("I", b, 60, MEM_MMAP)
                try: fcntl.ioctl(fd, VIDIOC_DQBUF, b, True)
                except BlockingIOError: continue
                idx, used = struct.unpack_from("I", b, 0)[0], struct.unpack_from("I", b, 8)[0]
                data = bytes(bufs[idx][:used])
                fcntl.ioctl(fd, VIDIOC_QBUF, b, True)  # tra buffer ngay
                if used < 4 or data[:2] != b"\xff\xd8": continue
                t_last = now; cnt += 1
                crc = zlib.crc32(data)
                # man hinh dung yen -> MS2109 xuat frame giong het nhau: bo qua, khong ton bang thong
                if crc != self._crc or now - t_pub > 2.0:
                    self._crc, t_pub = crc, now; nchg += 1; nbytes += used
                    with self.cv:
                        self.latest = data; self.seq += 1; self.err = None
                        self.cv.notify_all()
                if now - t0 >= 1.0:
                    dt = now - t0
                    self.fps = round(cnt / dt, 1); self.chg = round(nchg / dt, 1)
                    self.kbps = int(nbytes * 8 / 1000 / dt)
                    t0, cnt, nbytes, nchg = now, 0, 0, 0
        finally:
            try: fcntl.ioctl(fd, VIDIOC_STREAMOFF, struct.pack("I", BUF_CAPTURE))
            except OSError: pass
            for m in bufs:
                try: m.close()
                except Exception: pass
            os.close(fd)

# ---------------- input -> hidfwd ----------------
_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
def fwd(cmd):
    try: _udp.sendto(cmd.encode(), FWD)
    except OSError: pass

def udc_state():
    try:
        with open(UDC_STATE) as f: return f.read().strip()
    except OSError: return "unknown"

def _i(s, lo=-100000, hi=100000):
    return max(lo, min(hi, int(s)))

def handle_input(msg):
    """Lenh text gon tu trinh duyet -> lenh hidfwd."""
    c, _, a = msg.partition(" ")
    p = a.split()
    if c == "m":   # m x y b w  (tuyet doi 0..32767)
        fwd(f"abs {_i(p[0],0,32767)} {_i(p[1],0,32767)} {_i(p[2],0,7)} {_i(p[3],-127,127) if len(p)>3 else 0}")
    elif c == "r": # r dx dy b w (tuong doi)
        fwd(f"rel {_i(p[0])} {_i(p[1])} {_i(p[2],0,7)} {_i(p[3],-127,127) if len(p)>3 else 0}")
    elif c == "d": fwd(f"hdown {_i(p[0],0,255)}")
    elif c == "u": fwd(f"hup {_i(p[0],0,255)}")
    elif c == "c": fwd("combo " + a[:64])
    elif c == "t":
        for i in range(0, len(a), 500): fwd("text " + a[i:i+500])
    elif c == "x": fwd("relall")
    else: return False
    return True

# ---------------- WebSocket ----------------
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

class WSConn:
    def __init__(self, h):
        self.sock, self.rf = h.connection, h.rfile
        self.slock = threading.Lock()
        self.cv = threading.Condition()
        self.inflight, self.win, self.alive = 0, 2, True
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 512 * 1024)
        except OSError: pass

    def send(self, op, data):
        n = len(data)
        if n < 126: hdr = struct.pack(">BB", 0x80 | op, n)
        elif n < 65536: hdr = struct.pack(">BBH", 0x80 | op, 126, n)
        else: hdr = struct.pack(">BBQ", 0x80 | op, 127, n)
        with self.slock: self.sock.sendall(hdr + data)

    def text(self, s): self.send(1, s.encode())

    def _read(self, n):
        b = self.rf.read(n)
        if len(b) < n: raise EOFError
        return b

    def recv(self):
        buf, op0 = b"", None
        while True:
            h = self._read(2)
            fin, op, ln = h[0] & 0x80, h[0] & 0x0F, h[1] & 0x7F
            if ln == 126: ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127: ln = struct.unpack(">Q", self._read(8))[0]
            if ln > 1 << 20: raise EOFError
            mask = self._read(4) if h[1] & 0x80 else None
            data = self._read(ln)
            if mask:
                m = int.from_bytes((mask * (ln // 4 + 1))[:ln], "big")
                data = (int.from_bytes(data, "big") ^ m).to_bytes(ln, "big") if ln else b""
            if op == 8: raise EOFError
            if op == 9: self.send(10, data); continue
            if op == 10: continue
            if op0 is None: op0 = op
            buf += data
            if fin: return op0, buf

    def video_loop(self, g):
        seq, t_stat = 0, 0
        try:
            while self.alive:
                with self.cv:
                    self.cv.wait_for(lambda: self.inflight < self.win or not self.alive, 0.5)
                    can = self.inflight < self.win
                now = time.time()
                if now - t_stat >= 1.0:
                    t_stat = now
                    self.text(json.dumps({"t": "s", "fps": g.fps, "chg": g.chg, "kbps": g.kbps, "mode": g.mode,
                                          "modes": g.modes, "err": g.err, "udc": udc_state(), "win": self.win}))
                if not can: continue
                s, f = g.wait_frame(seq, 0.5)
                if s == seq or f is None: continue
                seq = s
                with self.cv: self.inflight += 1
                self.send(2, f)
        except (OSError, EOFError): pass
        finally:
            self.alive = False

    def serve(self, g):
        t = threading.Thread(target=self.video_loop, args=(g,), daemon=True); t.start()
        try:
            while self.alive:
                op, data = self.recv()
                if op != 1: continue
                msg = data.decode(errors="replace")
                if msg == "k":
                    with self.cv: self.inflight = max(0, self.inflight - 1); self.cv.notify()
                    continue
                try:
                    if handle_input(msg): continue
                    c, _, a = msg.partition(" ")
                    if c == "p": self.text("P " + a[:32])
                    elif c == "q":
                        w, h, f = a.split()[:3]; g.set_mode(w, h, f)
                    elif c == "win":
                        with self.cv: self.win = max(1, min(4, int(a))); self.cv.notify()
                except (ValueError, IndexError): pass
        except (OSError, EOFError): pass
        finally:
            self.alive = False
            with self.cv: self.cv.notify()
            fwd("relall")  # tranh ket phim khi mat ket noi
            t.join(2)

# ---------------- HTTP ----------------
grabber = None
PAGE_PATH = os.path.join(HERE, "kvm.html")

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers()
        if body: self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                with open(PAGE_PATH, "rb") as f: body = f.read()
            except OSError as e:
                return self._send(500, f"thiếu {PAGE_PATH}: {e}".encode())
            self._send(200, body, "text/html; charset=utf-8")
        elif path == "/ws":
            key = self.headers.get("Sec-WebSocket-Key")
            if not key or "websocket" not in self.headers.get("Upgrade", "").lower():
                return self._send(400, b"websocket only")
            acc = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", acc)
            self.end_headers(); self.wfile.flush()
            WSConn(self).serve(grabber)
            self.close_connection = True
        elif path == "/snap":
            _, f = grabber.wait_frame(-1, 0)
            if f: self._send(200, f, "image/jpeg",
                             {"Content-Disposition": f'inline; filename="kvm-{time.strftime("%Y%m%d-%H%M%S")}.jpg"'})
            else: self._send(503, b"no frame")
        elif path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=frm")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 256 * 1024)
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError: pass
            seq = 0
            try:
                while True:
                    s, f = grabber.wait_frame(seq, 15)
                    if s == seq or not f: break
                    seq = s
                    self.wfile.write(b"--frm\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(f)).encode() + b"\r\n\r\n" + f + b"\r\n")
            except (OSError, ValueError): pass
            self.close_connection = True
        elif path == "/health":
            self._send(200, json.dumps({"mode": grabber.mode, "fps": grabber.fps, "chg": grabber.chg, "kbps": grabber.kbps,
                                        "err": grabber.err, "udc": udc_state(), "modes": grabber.modes}).encode(),
                       "application/json")
        else:
            self._send(404, b"not found")

    def do_POST(self):  # tuong thich API cu
        if self.path != "/input": return self._send(404)
        try:
            n = int(self.headers.get("Content-Length", 0))
            d = json.loads(self.rfile.read(n) or b"{}")
            t = d.get("t")
            if t == "abs": handle_input(f"m {d.get('x',0)} {d.get('y',0)} {d.get('b',0)} {d.get('w',0)}")
            elif t == "kd": handle_input(f"d {d.get('u',0)}")
            elif t == "ku": handle_input(f"u {d.get('u',0)}")
            elif t == "combo": handle_input("c " + str(d.get("s", "")))
            elif t == "text": handle_input("t " + str(d.get("s", "")))
            elif t == "rel": handle_input("x")
            self._send(200, b"ok")
        except Exception as e:
            self._send(400, str(e).encode())

class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

def main():
    global grabber, DEV, PORT, FWD
    args = sys.argv[1:]
    while args:
        a = args.pop(0)
        if a == "--dev": DEV = args.pop(0)
        elif a == "--port": PORT = int(args.pop(0))
        elif a == "--fwd":
            h, _, p = args.pop(0).rpartition(":"); FWD = (h, int(p))
    mode = (1920, 1080, 60)
    try:
        with open(CONF) as f: mode = tuple(json.load(f)["mode"])
    except Exception: pass
    grabber = Grabber(DEV, mode)
    print(f"KVM web: http://0.0.0.0:{PORT}  (capture {DEV}, forward -> {FWD[0]}:{FWD[1]})", flush=True)
    Server(("0.0.0.0", PORT), H).serve_forever()

if __name__ == "__main__":
    main()
