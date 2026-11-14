#!/usr/bin/env python3
"""
kvmweb.py - Web KVM cho ZTE W105D/W103D (Armbian, S905X2). Ban port tu KVM cua W152D (bo o USB ao/ISO/ban goc).

- GET /        giao dien dieu khien (video + ban phim/chuot)
- GET /ws      WebSocket: video JPEG (delta, flow-control theo ack) hoac H.264 phan cung (xd-venc) + input
- GET /stream  MJPEG stream (tuong thich cu)
- GET /snap    1 frame JPEG
- GET /health  trang thai JSON
- GET /api     mo ta cac endpoint (cho agent)
- GET /usb     USB cam vao W105D -> may dich (usb-share.sh, LUN0): ext_dev, ext_plugged, ext_conf, ten LUN
- POST /usb    {"op":"ext_on|ext_off"} bat/tat chuyen USB (giu qua reboot) | {"op":"rescan|detach"}
- POST /cmd    lenh hidfwd tho, text hoac {"cmd":...}: key enter | combo ctrl+alt+del
               | text <s> | abs x y b | move dx dy | click left | wheel n | relall
- POST /input  JSON event (tuong thich cu; them t:"key" theo ten, t:"px" theo pixel /snap)
- GET /audio   WebSocket: am thanh HDMI cua MS2109 (mu-law 24kHz stereo, khung 20ms)
- GET /sys     suc khoe box (nhiet do, RAM, dia, dich vu, USB, den phim); POST /sys {op:replug|capture|hidfwd}
- GET/POST /macros  macro dung chung (luu /var/lib/xd-kvm/macros.json)
- GET /clip    clipboard may dich (qua agent) + lich su; POST /clip {op:set|install|uninstall|clear}
- POST /clip/agent  agent tren may dich bao clipboard / nhan text day sang (clip/agent.ps1, agent.py, m.html)
- /terminal, /api/sessions  terminal root (xdterm.py, dich vu xd-term) ; /debug.md huong dan cho agent

Input duoc chuyen thanh lenh UDP toi hidfwd.py (127.0.0.1:5115). Chay bang root (dich vu kvmweb).

Chay:  python3 kvmweb.py [--dev /dev/videoX] [--port 8090] [--fwd 127.0.0.1:5115]
"""
import os, sys, glob, shlex, secrets, hmac, http.client, fcntl, mmap, struct, select, socket, json, threading, time, base64, hashlib, zlib, re, subprocess, queue, urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(HERE, "kvmweb.json")
DEV, PORT, FWD = "/dev/v4l/by-id/usb-MACROSILICON_2109-video-index0", 8090, ("127.0.0.1", 5115)
UDC_STATE = "/sys/class/udc/ff400000.usb/state"         # W105D: dwc2 tren cong OTG
_MS = "/sys/kernel/config/usb_gadget/kvm/functions/mass_storage.usb0"
LUN0 = _MS + "/lun.0/file"                                # USB cam vao W105D -> may dich (usb-share.sh)
USB_SHARE = os.path.join(HERE, "usb-share.sh")
USB_STATE, USB_LOG, USB_OFF = "/run/kvm-usb-share", "/run/kvm-usb-share.log", "/var/lib/xd-kvm/usb-share.off"
LEDS_FILE = "/run/xd-hid-leds"                             # hidfwd: bit0 Num, bit1 Caps, bit2 Scroll
MACROS = "/var/lib/xd-kvm/macros.json"
ABSMODE = "/var/lib/xd-kvm/absmode"                        # hidfwd: "mouse" (hidg3) | "pen" (hidg2)

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

# ---------------- delta MJPEG ----------------
# MS2109 ghi restart marker (DRI) sau moi MCU -> moi doan entropy doc lap.
# Chi gui cac doan thay doi; trinh duyet ghep lai thanh JPEG day du.
# Tach doan tang dan: tim vung byte khac frame truoc (memcmp) roi chi tach vung do.
from bisect import bisect_left, bisect_right
from itertools import accumulate
from collections import deque
_RST_SPLIT = re.compile(rb"(?<=\xff[\xd0-\xd7])")

def jpeg_hlen(data):
    """Do dai header (den het SOS) neu JPEG co DRI, nguoc lai 0."""
    i, n, dri = 2, len(data), False
    while i + 4 <= n:
        if data[i] != 0xFF: return 0
        m = data[i + 1]
        L = (data[i + 2] << 8) | data[i + 3]
        if m == 0xDD: dri = True
        if m == 0xDA: return i + 2 + L if dri else 0
        i += 2 + L
    return 0

def _cpre(a, b, n):
    if a[:n] == b[:n]: return n
    lo, hi = 0, n
    while hi - lo > 64:
        m = (lo + hi) // 2
        if a[lo:m] == b[lo:m]: lo = m
        else: hi = m
    while lo < hi and a[lo] == b[lo]: lo += 1
    return lo

def _csuf(a, b, n):
    la, lb = len(a), len(b)
    if a[la - n:] == b[lb - n:]: return n
    lo, hi = 0, n
    while hi - lo > 64:
        m = (lo + hi) // 2
        if a[la - m:la - lo] == b[lb - m:lb - lo]: lo = m
        else: hi = m
    while lo < hi and a[la - 1 - lo] == b[lb - 1 - lo]: lo += 1
    return lo

class Frame:
    __slots__ = ("data", "hlen", "segs", "starts", "prev", "lo", "hi")
    def __init__(self, data):
        self.data, self.hlen = data, jpeg_hlen(data)
        self.segs = self.starts = self.prev = self.lo = self.hi = None
    def ensure_segs(self):
        if self.segs is None and self.hlen:
            self.segs = _RST_SPLIT.split(self.data[self.hlen:])
            self.starts = list(accumulate([len(x) for x in self.segs[:-1]], initial=self.hlen))
        return self.segs

def frame_diff(f, p):
    """Dat f.segs/f.lo/f.hi (vung doan khac p). f.lo=None: thay doi lon/khong so duoc."""
    a, b = p.data, f.data
    if not f.hlen or f.hlen != p.hlen or a[:f.hlen] != b[:f.hlen]: return
    n0 = min(len(a), len(b))
    pre = _cpre(a, b, n0)
    if pre == len(a) == len(b):
        f.segs, f.starts, f.lo, f.hi = p.segs, p.starts, 0, 0; return
    suf = _csuf(a, b, n0 - pre)
    if len(a) - pre - suf > 0.6 * len(a) or len(b) - pre - suf > 0.6 * len(b): return
    ps = p.ensure_segs(); st = p.starts; n = len(ps)
    k = max(0, bisect_right(st, pre) - 1)
    t0 = max(k, bisect_left(st, len(a) - suf))
    end = len(b) - (len(a) - st[t0]) if t0 < n else len(b)
    if end < st[k]: return
    mid = _RST_SPLIT.split(b[st[k]:end])
    if t0 < n and mid and mid[-1] == b"": mid.pop()
    if k + len(mid) + (n - t0) != n: return
    d = len(b) - len(a)
    f.segs = ps[:k] + mid + ps[t0:]
    f.starts = st[:k] + (list(accumulate([len(x) for x in mid[:-1]], initial=st[k])) if mid else []) + [x + d for x in st[t0:]]
    f.lo, f.hi = k, k + len(mid)

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
        self._usbreset, self.resets = None, 0     # Event khi co yeu cau reset USB card; so lan da reset
        self._flock, self._frames, self._order, self._lastf = threading.Lock(), {}, deque(), None
        self.start()

    def frame(self, seq, data):
        """Frame (co diff so voi frame truoc), dung chung cho moi client."""
        with self._flock:
            f = self._frames.get(seq)
            if f is not None: return f
            f = Frame(data); p = self._lastf
            if p is not None:
                f.prev = p
                try: frame_diff(f, p)
                except Exception: f.lo = None
            self._lastf = f; self._frames[seq] = f; self._order.append(seq)
            while len(self._order) > 24:
                self._frames.pop(self._order.popleft(), None)
                nf = self._frames.get(self._order[0])
                if nf is not None: nf.prev = None
            return f

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

    def usb_reset(self, timeout=15):
        """Reset USB rieng card capture (MS2109 co luc treo: STREAMON OK nhung khong tra frame, mo lai fd khong cuu duoc).
        Grabber dong fd, ioctl USBDEVFS_RESET, cho uvcvideo gan lai roi mo lai. Khong dung gi toi may dich."""
        ev = threading.Event(); seq0 = self.seq
        self._usbreset = ev; self._reset = True
        if not ev.wait(timeout): return "reset quá lâu"
        if isinstance(getattr(ev, "err", None), str): return ev.err
        t = time.time() + 6                       # cho frame dau tien
        while time.time() < t and self.seq == seq0: time.sleep(0.2)
        return None if self.seq != seq0 else "đã reset nhưng chưa có hình (kiểm tra máy đích/cáp HDMI)"

    def _do_usb_reset(self):
        ev, self._usbreset = self._usbreset, None
        self.err, self.fps = "đang reset card capture…", 0.0
        try:
            v = os.path.basename(os.path.realpath(self.dev))
            d = os.path.dirname(os.path.realpath(f"/sys/class/video4linux/{v}/device"))   # 1-1.3:1.0 -> 1-1.3
            bus, dev = int(open(d + "/busnum").read()), int(open(d + "/devnum").read())
            fd = os.open("/dev/bus/usb/%03d/%03d" % (bus, dev), os.O_WRONLY)
            try: fcntl.ioctl(fd, 0x5514, 0)       # USBDEVFS_RESET
            finally: os.close(fd)
            self.resets += 1
            print(f"capture: reset USB {os.path.basename(d)} ({bus}/{dev})", flush=True)
            t = time.time() + 8                   # cho uvcvideo gan lai
            time.sleep(0.5)
            while time.time() < t and not os.path.exists(self.dev): time.sleep(0.2)
            time.sleep(0.5)
        except Exception as e:
            ev.err = f"reset lỗi: {e}"
        ev.set()

    def run(self):
        while True:
            try: self._capture()
            except Exception as e:
                self.err, self.fps = str(e), 0.0
                if not self._usbreset: time.sleep(1.5)
            if self._usbreset: self._do_usb_reset()

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

def _blk_desc(name):
    b = "/sys/block/" + name
    d = {"dev": "/dev/" + name}
    try: d["gib"] = round(int(_rd(b + "/size") or 0) * 512 / 2**30, 1)
    except ValueError: d["gib"] = 0
    d["model"] = " ".join(x for x in (_rd(b + "/device/vendor"), _rd(b + "/device/model")) if x)
    return d

def usb_status():
    """USB cam vao W105D chuyen sang may dich (LUN0, usb-share.sh). Cung kieu du lieu 'ext_*' voi trang KVM cua W152D."""
    plug = []
    for b in sorted(glob.glob("/sys/block/sd*")):
        real = os.path.realpath(b)
        if "/usb" not in real: continue
        try:
            if int(_rd(b + "/size") or 0) == 0: continue
        except ValueError: continue
        plug.append(_blk_desc(os.path.basename(b)))
    cur = _rd(LUN0)
    st = {"udc": udc_state(), "mode": "none", "exists": False, "ext": cur, "ext_conf": "off" if os.path.exists(USB_OFF) else "auto",
          "ext_plugged": plug, "ext_dev": None, "names": [_rd(_MS + "/lun.0/inquiry_string")[8:24].strip()]}
    if cur:
        n = os.path.basename(os.path.realpath(cur))
        st["ext_dev"] = _blk_desc(n) if os.path.isdir("/sys/block/" + n) else {"dev": cur, "gib": 0, "model": ""}
    try:
        with open(USB_LOG) as f: st["log"] = "".join(f.readlines()[-12:])
    except OSError: st["log"] = ""
    return st

def usb_op(op, dev="", prealloc=False):
    """usb-share.sh on|off|rescan|detach (kvmweb chay root)."""
    args = {"ext_on": ["on"], "ext_off": ["off"], "rescan": ["rescan"], "detach": ["detach"]}.get(op)
    if not args: return None
    try:
        r = subprocess.run([USB_SHARE] + args, capture_output=True, text=True, timeout=30)
        ok = r.returncode == 0
        if op == "ext_on":
            time.sleep(0.3); cur = _rd(LUN0)
            out = "Đã gắn " + cur if cur else "Đã bật – chưa có USB nào cắm vào W105D"
        elif op == "ext_off": out = "Đã gỡ USB khỏi máy đích"
        else: out = (r.stdout + r.stderr).strip()[-300:] or "ok"
        return {"rc": 0 if ok else r.returncode, "out": out if ok else (r.stdout + r.stderr).strip()[-300:]}
    except Exception as e:
        return {"rc": -1, "out": str(e)}

def _rd(p):
    try:
        with open(p) as f: return f.read().strip()
    except OSError: return ""

def _wr(p, v):
    with open(p, "w") as f: f.write(v)

def leds():
    v = _rd(LEDS_FILE)
    return int(v) if v.isdigit() else None

def gadget_restart():
    """Tao lai gadget (gadget-setup.sh), roi gan lai USB cho may dich."""
    r = subprocess.run(["systemctl", "restart", "usb-kvm-gadget"], capture_output=True, text=True, timeout=40)
    if r.returncode != 0: return "cắm lại USB lỗi: " + (r.stdout + r.stderr).strip()[-200:]
    subprocess.run([USB_SHARE, "rescan"], capture_output=True, timeout=30)
    return None

# ---------------- suc khoe box ----------------
SVCS = ["usb-kvm-gadget", "hidfwd", "kvmweb", "xd-term", "tailscaled", "sremote", "net-fallback"]

def sys_status():
    d = {}
    try:
        t = int(_rd("/sys/class/thermal/thermal_zone0/temp"))
        d["temp"] = round(t / 1000, 1) if t > 1000 else t     # kernel chuan ghi mC
    except ValueError: d["temp"] = None
    d["load"] = [round(x, 2) for x in os.getloadavg()]
    mi = {}
    try:
        for ln in open("/proc/meminfo"):
            k, v = ln.split(":", 1); mi[k] = int(v.split()[0]) * 1024
    except (OSError, ValueError): pass
    d["mem"] = {"total": mi.get("MemTotal", 0), "avail": mi.get("MemAvailable", 0)}
    try: d["uptime"] = int(float(_rd("/proc/uptime").split()[0]))
    except (ValueError, IndexError): d["uptime"] = 0
    d["disk"] = {}
    for n, p in (("root", "/"),):
        try:
            v = os.statvfs(p); d["disk"][n] = {"total": v.f_blocks * v.f_frsize, "free": v.f_bavail * v.f_frsize}
        except OSError: pass
    try:
        r = subprocess.run(["systemctl", "is-active"] + SVCS, capture_output=True, text=True, timeout=5)
        d["svc"] = dict(zip(SVCS, r.stdout.split()))
    except Exception: d["svc"] = {}
    d["udc"] = udc_state()
    d["speed"] = _rd(os.path.join(os.path.dirname(UDC_STATE), "current_speed"))
    d["leds"] = leds()
    d["host"] = socket.gethostname()
    if grabber: d["cap"] = {"mode": grabber.mode, "fps": grabber.fps, "err": grabber.err}
    d["audio"] = {"listeners": len(audio.subs), "err": audio.err}
    return d

def sys_op(op):
    try:
        if op == "replug":   # tao lai ca gadget (gadget-setup.sh) -> may dich thay rut/cam lai USB
            e = gadget_restart()
            return {"rc": 1 if e else 0, "out": e or "đã cắm lại USB"}
        if op == "capture":        # reset USB card capture (cuu MS2109 treo), cho toi khi co frame
            e = grabber.usb_reset()
            return {"rc": 1 if e else 0, "out": e or "đã reset card capture, có hình lại"}
        if op == "hidfwd":
            r = subprocess.run(["systemctl", "restart", "hidfwd"], capture_output=True, text=True, timeout=20)
            return {"rc": r.returncode, "out": (r.stdout + r.stderr).strip()[-300:] or "đã khởi động lại hidfwd"}
    except Exception as e:
        return {"rc": -1, "out": str(e)}
    return None

# ---------------- macro ----------------
def macros_load():
    try:
        with open(MACROS) as f: m = json.load(f)
        return m if isinstance(m, list) else []
    except (OSError, ValueError): return []

def macros_save(m):
    if not isinstance(m, list) or len(m) > 100: raise ValueError("tối đa 100 macro")
    out = []
    for x in m:
        name, script = str(x.get("name", "")).strip()[:48], str(x.get("script", ""))[:8000]
        if not name: raise ValueError("macro thiếu tên")
        out.append({"name": name, "script": script})
    os.makedirs(os.path.dirname(MACROS), exist_ok=True)
    tmp = MACROS + ".tmp"
    with open(tmp, "w") as f: json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, MACROS)
    return out

# ---------------- clipboard (agent tren may dich) ----------------
# Agent (clip/agent.ps1 | agent.py | m.html) POST /clip/agent moi ~0,6 s: gui text khi clipboard may dich doi,
# nhan lai "push" (text trang KVM muon dua vao clipboard may dich) khi seq cua no cu hon.
CLIP_DIR = os.path.join(HERE, "clip")
CLIP_MAX = 500000
clip_lk = threading.Lock()
CLIP = {"seq": 0, "text": "", "ts": 0, "src": ""}           # moi nhat tu may dich
PUSH = {"seq": 0, "text": "", "ts": 0}                      # trang KVM -> may dich
HIST, AGENTS, INSTALL = [], {}, {"state": "idle"}

def clip_bases(host_hdr=""):
    """Cac URL may dich co the dung de goi lai W105D: IP LAN truoc, roi Tailscale, roi Host trang dang dung."""
    out = []
    try:
        r = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"], capture_output=True, text=True, timeout=3)
        ips = re.findall(r"^\d+:\s+(\S+)\s+inet ([\d.]+)/", r.stdout, re.M)
        ips = [x for x in ips if not re.match(r"(docker|br-|veth|virbr|lxc)", x[0])]
        ips.sort(key=lambda x: (x[0].startswith("tailscale"), x[0] != "eth0"))
        out = [f"http://{ip}:{PORT}" for _, ip in ips]
    except (OSError, subprocess.SubprocessError): pass
    h = host_hdr.strip()
    if re.fullmatch(r"[\w.\-]+(:\d+)?", h) and not re.match(r"(localhost|127\.)", h) and not any(u.endswith("//" + h) for u in out): out.append("http://" + h)
    return out

NONCES = {}                                                 # link cai agent dung 1 lan cai (hieu luc 10 phut)

def clip_nonce():
    n = secrets.token_hex(6); now = time.time()
    for k in [k for k, t in NONCES.items() if t < now]: NONCES.pop(k, None)
    NONCES[n] = now + 600
    return n

def clip_nonce_ok(n): return NONCES.get(n, 0) > time.time()

def clip_script(name, base, host_hdr, nonce=""):
    """Agent/bo cai: nhung danh sach URL cua box, khoa agent (__KEY__) va duong dan tai (__DL__)."""
    with open(os.path.join(CLIP_DIR, name)) as f: s = f.read()
    s = s.replace("__KEY__", clip_key()).replace("__DL__", f"/clip/i/{nonce}" if nonce else "/clip")
    bases = [base] + [b for b in clip_bases(host_hdr) if b != base] if base else clip_bases(host_hdr)
    q = ", ".join("'" + b.replace("'", "") + "'" for b in bases)
    return s.replace("__BASES__", q).replace("__BASE__", (base or bases[0]).replace("'", ""))

def clip_agent(d, ip):
    if not isinstance(d, dict): raise ValueError("bad body")
    aid = re.sub(r"[^\w.\-]", "", str(d.get("id", "")))[:40] or ip
    now = time.time()
    with clip_lk:
        a = AGENTS.setdefault(aid, {"first": now})
        a.update(id=aid, host=str(d.get("host", ""))[:64], user=str(d.get("user", ""))[:64], os=str(d.get("os", ""))[:16],
                 ip=ip, seen=now, err=str(d.get("err", ""))[:200], seq=d.get("seq"))
        t = d.get("text")
        if isinstance(t, str) and t and t != CLIP["text"]:
            t = t[:CLIP_MAX]
            CLIP.update(seq=CLIP["seq"] + 1, text=t, ts=now, src=aid)
            HIST.insert(0, {"seq": CLIP["seq"], "text": t[:20000], "ts": now, "src": aid}); del HIST[10:]
        r = {"seq": PUSH["seq"], "push": None}
        if a.pop("uninstall", False): r["uninstall"] = True; AGENTS.pop(aid, None)
        s = d.get("seq")
        if s != PUSH["seq"] and PUSH["seq"] and (s != -1 or a["os"] in ("android", "ios", "web") or now - PUSH["ts"] < 15):
            r["push"] = PUSH["text"]
        return r

def clip_status():
    now = time.time()
    with clip_lk:
        ags = [dict(a, online=now - a["seen"] < 5, ago=round(now - a["seen"]), got=a.get("seq") == PUSH["seq"])
               for a in sorted(AGENTS.values(), key=lambda a: -a["seen"])]
        return {"clip": dict(CLIP), "push": {"seq": PUSH["seq"], "ts": PUSH["ts"], "len": len(PUSH["text"])},
                "hist": list(HIST), "agents": ags, "install": dict(INSTALL), "bases": clip_bases(), "now": now}

def clip_online():
    now = time.time()
    with clip_lk: return sum(1 for a in AGENTS.values() if now - a["seen"] < 5)

def clip_set(t):
    if not isinstance(t, str) or not t: return "text trống"
    with clip_lk: PUSH.update(seq=PUSH["seq"] + 1, text=t[:CLIP_MAX], ts=time.time())

def _install_run(osn, base, nonce):
    """Go lenh cai agent vao may dich bang ban phim HID (Win+R / Ctrl+Alt+T / thanh dia chi)."""
    bl = [base] + [b for b in clip_bases() if b != base]
    try:
        if osn == "windows":
            lst = ",".join(f"'{b}'" for b in bl[:3])
            cmd = (f'powershell -NoP -EP Bypass -NoExit -c "foreach($b in {lst}){{try{{$s=irm ($b+\'/clip/i/{nonce}/install.ps1?b=\'+$b) '
                   f'-TimeoutSec 4}}catch{{continue}};iex $s;break}}"')
            steps = [("combo win+r", 1.3), ("text " + cmd, 0), ("key enter", 0)]
        elif osn == "linux":
            lst = " ".join(bl[:3])
            cmd = (f'for b in {lst}; do curl -fsSm4 "$b/clip/i/{nonce}/install.sh?b=$b" -o /tmp/xdclip.sh && break; '
                   f'done; sh /tmp/xdclip.sh; rm -f /tmp/xdclip.sh')
            steps = [("combo ctrl+alt+t", 2.0), ("text " + cmd, 0), ("key enter", 0)]
        elif osn == "android":
            steps = [("text " + bl[0] + f"/clip/i/{nonce}/m", 0), ("key enter", 0)]
        else: raise ValueError(osn)
        INSTALL.update(state="typing", os=osn, ts=time.time(), chars=sum(len(s) for s, _ in steps))
        for s, w in steps:
            fwd(s)
            if w: time.sleep(w)
        time.sleep(sum(len(s) for s, _ in steps) * 0.045 + 1)
        INSTALL.update(state="done")
    except Exception as e:
        INSTALL.update(state="error", err=str(e))

def clip_install(osn, host_hdr):
    if osn not in ("windows", "linux", "android"): return "os: windows|linux|android"
    if INSTALL.get("state") == "typing" and time.time() - INSTALL.get("ts", 0) < 30: return "đang gõ lệnh cài"
    bl = clip_bases(host_hdr)
    if not bl: return "W105D chưa có địa chỉ mạng"
    INSTALL.clear(); INSTALL.update(state="typing", os=osn, ts=time.time())
    threading.Thread(target=_install_run, args=(osn, bl[0], clip_nonce()), daemon=True).start()

def clip_uninstall(aid):
    with clip_lk:
        if aid not in AGENTS: return "không có agent này"
        if time.time() - AGENTS[aid]["seen"] > 10: AGENTS.pop(aid)   # da mat ket noi: chi xoa khoi danh sach
        else: AGENTS[aid]["uninstall"] = True

# ---------------- am thanh HDMI (MS2109) ----------------
class AudioHub:
    """1 tien trinh arecord dung chung cho moi nguoi nghe; chi chay khi co nguoi nghe."""
    RATE, CH, CHUNK = 24000, 2, 960          # mu-law 1 byte/mau -> 960 B = 20 ms stereo
    def __init__(self):
        self.subs, self.lock, self.th, self.err = set(), threading.Lock(), None, None
    def card(self):
        m = re.search(r"^\s*\d+ \[(\S+)\s*\]:[^\n]*\n\s*MACROSILICON", _rd("/proc/asound/cards") + "\n", re.M)
        return m.group(1) if m else None
    def add(self, q):
        with self.lock:
            self.subs.add(q)
            if not (self.th and self.th.is_alive()):
                self.th = threading.Thread(target=self.run, daemon=True); self.th.start()
    def remove(self, q):
        with self.lock: self.subs.discard(q)
    def run(self):
        while self.subs:
            c = self.card()
            if not c:
                self.err = "không thấy card âm thanh MS2109"; time.sleep(2); continue
            p = subprocess.Popen(["arecord", "-q", "-D", f"plughw:CARD={c},DEV=0", "-f", "MU_LAW", "-r", str(self.RATE),
                                  "-c", str(self.CH), "-t", "raw", "-B", "120000", "-F", "20000"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.err = None
            try:
                while self.subs:
                    b = p.stdout.read(self.CHUNK)
                    if not b: break
                    for q in list(self.subs):
                        try: q.put_nowait(b)
                        except queue.Full: pass          # client cham: bo khung
            finally:
                p.terminate()
                try: _, e = p.communicate(timeout=2)
                except Exception: e = b""
                if self.subs and e: self.err = e.decode(errors="replace").strip()[-200:]
            if self.subs: time.sleep(1)

audio = AudioHub()

# ---------------- H.264 phan cung (xd-venc: MJPEG -> amvenc_avc) ----------------
VENC = os.environ.get("XD_VENC", "/usr/local/bin/xd-venc")

class H264Hub:
    """1 tien trinh xd-venc dung chung; chi chay khi co nguoi xem H.264. Dua khung JPEG moi nhat vao stdin
    (encoder ban -> bo khung cu), phat access unit (Annex-B) cho tung client qua queue rieng."""
    def __init__(self):
        self.subs, self.lock, self.proc, self.err = set(), threading.Lock(), None, None
        self.idr, self.kbps, self.newkbps, self.last_key = False, int(os.environ.get("XD_VENC_KBPS", "6000")), None, None
        self.fps, self.bytes, self.size = 0.0, 0, None
    def avail(self):
        return os.access(VENC, os.X_OK) and os.path.exists("/dev/amvenc_avc")
    def add(self, q):
        with self.lock:
            self.subs.add(q); self.idr = True
            if not (self.proc and self.proc.poll() is None):
                self._start()
    def remove(self, q):
        with self.lock:
            self.subs.discard(q)
            if not self.subs and self.proc:
                try: self.proc.stdin.close()
                except OSError: pass
                self.proc = None
    def want_idr(self): self.idr = True
    def set_kbps(self, k):
        k = max(300, min(20000, int(k)))
        if k != self.kbps: self.kbps = k; self.newkbps = k
    def _start(self):
        try:
            p = subprocess.Popen([VENC, str(self.kbps), "30"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, bufsize=0)
        except OSError as e:
            self.err = str(e); return
        self.proc, self.err = p, None
        threading.Thread(target=self._feed, args=(p,), daemon=True).start()
        threading.Thread(target=self._read, args=(p,), daemon=True).start()
        threading.Thread(target=self._log, args=(p,), daemon=True).start()
    def _log(self, p):
        for ln in p.stderr:
            ln = ln.decode(errors="replace").strip()
            if ln: print(ln, flush=True); self.err = ln if "loi" in ln else self.err
    def _feed(self, p):
        seq = 0
        try:
            while self.proc is p and p.poll() is None:
                s, f = grabber.wait_frame(seq, 1.0)
                if s == seq or f is None: continue
                seq = s
                if self.newkbps:
                    k, self.newkbps = self.newkbps, None
                    p.stdin.write(struct.pack("<I", 0x40000000 | k))
                hdr = len(f) | (0x80000000 if self.idr else 0)
                self.idr = False
                p.stdin.write(struct.pack("<I", hdr) + f)   # pipe day = encoder cham -> lan sau lay khung moi nhat
        except (OSError, ValueError): pass
    def _read(self, p):
        t0, n, nb = time.time(), 0, 0
        try:
            while True:
                h = p.stdout.read(9)
                if len(h) < 9: break
                ln, key, w, hh = struct.unpack("<IBHH", h)
                data = p.stdout.read(ln)
                if len(data) < ln: break
                if key: self.last_key = time.time()
                self.size = (w, hh)
                msg = b"\x03" + struct.pack(">BHH", key, w, hh) + data
                n += 1; nb += ln
                now = time.time()
                if now - t0 >= 1.0: self.fps, self.bytes, t0, n, nb = round(n / (now - t0), 1), int(nb * 8 / 1000 / (now - t0)), now, 0, 0
                with self.lock: subs = list(self.subs)
                for q in subs:
                    try: q.put_nowait((key, msg))
                    except queue.Full: pass
        finally:
            with self.lock:
                if self.proc is p: self.proc = None
            try: p.kill()
            except OSError: pass

h264 = H264Hub()

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
    elif c == "am" and a.strip() in ("mouse", "pen"): fwd("absmode " + a.strip())
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
        self.base, self.delta = None, True   # frame client dang co
        self.codec = "mjpeg"                  # "h264": nhan access unit tu H264Hub thay cho JPEG
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

    def encode(self, cur):
        """b'\x01'+JPEG (day du) | b'\x02'+runs (chi doan thay doi) | None (khong doi)."""
        base, self.base = self.base, cur
        full = b"\x01" + cur.data
        if base is None or not cur.hlen: return full
        lo = hi = None; x, depth = cur, 0
        while x is not base:                       # hop vung thay doi tu frame client co -> hien tai
            if x is None or x.lo is None or depth > 32: return full
            lo = x.lo if lo is None else min(lo, x.lo)
            hi = x.hi if hi is None else max(hi, x.hi)
            x, depth = x.prev, depth + 1
        if lo is None or hi <= lo: return None
        cs, bs = cur.ensure_segs(), base.ensure_segs()
        if cs is None or bs is None or len(cs) != len(bs): return full
        ch = [i for i in range(lo, hi) if cs[i] != bs[i]]
        if not ch: return None
        runs, st, en = [], ch[0], ch[0]
        for i in ch[1:]:
            if i - en <= 4: en = i               # gop cac doan gan nhau
            else: runs.append((st, en)); st = en = i
        runs.append((st, en))
        parts, size = [b"\x02", struct.pack(">I", len(runs))], 0
        for a, b in runs:
            blob = b"".join(cs[a:b + 1]); size += len(blob)
            parts.append(struct.pack(">III", a, b - a + 1, len(blob))); parts.append(blob)
        if size > len(cur.data) * 0.7: return full
        return b"".join(parts)

    def h264_loop(self, g):
        """Gui access unit H.264; client cham (ack khong kip) -> bo den IDR ke tiep thay vi xep hang."""
        q = queue.Queue(maxsize=60); h264.add(q); skip, t_stat = True, 0
        try:
            while self.alive and self.codec == "h264":
                now = time.time()
                if now - t_stat >= 1.0:
                    t_stat = now
                    self.text(json.dumps({"t": "s", "fps": g.fps, "chg": g.chg, "kbps": g.kbps, "mode": g.mode,
                                          "modes": g.modes, "err": g.err or h264.err, "udc": udc_state(), "win": self.win,
                                          "leds": leds(), "clip": CLIP["seq"], "ag": clip_online(), "am": _rd(ABSMODE) or "mouse",
                                          "codec": "h264", "venc": {"fps": h264.fps, "kbps": h264.bytes, "target": h264.kbps}}))
                try: key, msg = q.get(timeout=0.5)
                except queue.Empty: continue
                if self.inflight > 12 or q.qsize() > 12:      # tut qua xa (~0,4 s) -> bo, cho IDR
                    if not skip: h264.want_idr()
                    skip = True
                    with self.cv: self.inflight = 0          # coi nhu client da bo cac khung dang gui
                    while not q.empty():
                        try: q.get_nowait()
                        except queue.Empty: break
                    continue
                if skip:
                    if not key: continue
                    skip = False
                with self.cv: self.inflight += 1
                self.send(2, msg)
        finally:
            h264.remove(q)
    def video_loop(self, g):
        seq, t_stat = 0, 0
        try:
            while self.alive:
                if self.codec == "h264":
                    self.h264_loop(g); self.base = None; continue
                with self.cv:
                    self.cv.wait_for(lambda: self.inflight < self.win or not self.alive, 0.5)
                    can = self.inflight < self.win
                now = time.time()
                if now - t_stat >= 1.0:
                    t_stat = now
                    self.text(json.dumps({"t": "s", "fps": g.fps, "chg": g.chg, "kbps": g.kbps, "mode": g.mode,
                                          "modes": g.modes, "err": g.err, "udc": udc_state(), "win": self.win,
                                          "leds": leds(), "clip": CLIP["seq"], "ag": clip_online(), "am": _rd(ABSMODE) or "mouse"}))
                if not can: continue
                s, f = g.wait_frame(seq, 0.5)
                if s == seq or f is None: continue
                seq = s
                msg = self.encode(g.frame(s, f)) if self.delta else b"\x01" + f
                if msg is None: continue          # khong co gi thay doi
                with self.cv: self.inflight += 1
                self.send(2, msg)
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
                    elif c == "f": self.base = None
                    elif c == "delta": self.delta = a.strip() != "0"; self.base = None
                    elif c == "codec":
                        want = "h264" if a.strip() == "h264" and h264.avail() else "mjpeg"
                        self.codec = want; self.text(json.dumps({"t": "codec", "codec": want, "avail": h264.avail()}))
                    elif c == "kbps": h264.set_kbps(int(a))
                    elif c == "idr": h264.want_idr()
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

# ---------------- xac thuc: token moi cho moi lan cap nguon ----------------
# /run la tmpfs -> mat khi mat nguon/reboot -> kvmweb tao token moi, cho "Claim" 1 lan tren trang dang nhap.
# Dang nhap: cookie xdkvm (HMAC cua token) cho trinh duyet; agent dung "Authorization: Bearer <token>".
AUTH_FILE = os.environ.get("XD_AUTH_FILE", "/run/xd-kvm/auth.json")
CLIP_KEY_FILE = os.environ.get("XD_CLIP_KEY", "/var/lib/xd-kvm/clip.key")     # khoa rieng cho agent clipboard (song qua reboot)
TERM_UP = ("127.0.0.1", int(os.environ.get("XD_TERM_PORT", "8092")))                  # xd-term (xdterm.py)
_auth, _auth_mt, _auth_lk, _claim_lk = {}, [None], threading.Lock(), threading.Lock()
_fails = {}
PUBLIC = ("/login", "/auth/state", "/auth/claim", "/auth/login", "/auth/logout", "/favicon.ico", "/clip/agent", "/cli/xdkvm.js")

def _tok():
    a = "abcdefghjkmnpqrstuvwxyz23456789"
    return "xd-" + "-".join("".join(secrets.choice(a) for _ in range(5)) for _ in range(4))

def auth_save(d):
    os.makedirs(os.path.dirname(AUTH_FILE), mode=0o700, exist_ok=True)
    tmp = AUTH_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f: json.dump(d, f)
    os.replace(tmp, AUTH_FILE)

def auth_get():
    """Doc lai file khi doi (xd-kvm-token reset tu ssh); tao token moi neu chua co (vua cap nguon)."""
    with _auth_lk:
        try: mt = os.stat(AUTH_FILE).st_mtime_ns
        except OSError: mt = None
        if mt is None or mt != _auth_mt[0]:
            d = None
            if mt is not None:
                try:
                    with open(AUTH_FILE) as f: d = json.load(f)
                    if not (isinstance(d, dict) and d.get("token") and d.get("key")): d = None
                except (OSError, ValueError): d = None
            if d is None:
                d = {"token": _tok(), "key": secrets.token_hex(16), "claimed": False, "created": time.time()}
                auth_save(d)
            _auth.clear(); _auth.update(d)
            try: _auth_mt[0] = os.stat(AUTH_FILE).st_mtime_ns
            except OSError: _auth_mt[0] = None
        return dict(_auth)

def auth_set(**kw):
    d = auth_get(); d.update(kw); auth_save(d)
    with _auth_lk: _auth_mt[0] = None
    return auth_get()

def cookie_val(a): return hmac.new(a["key"].encode(), a["token"].encode(), hashlib.sha256).hexdigest()[:40]

def clip_key():
    try:
        with open(CLIP_KEY_FILE) as f:
            k = f.read().strip()
            if len(k) >= 16: return k
    except OSError: pass
    k = secrets.token_hex(16)
    os.makedirs(os.path.dirname(CLIP_KEY_FILE), exist_ok=True)
    fd = os.open(CLIP_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f: f.write(k)
    return k

def too_many(ip):
    now = time.time()
    l = [t for t in _fails.get(ip, []) if now - t < 300]
    _fails[ip] = l
    return len(l) >= 10

def debug_md(host_hdr):
    """GET /debug.md - huong dan cho agent (sinh moi lan tai: dia chi, trang thai, phien dang mo)."""
    h = host_hdr if re.fullmatch(r"[\w.\-:\[\]]+", host_hdr or "") else f"{(clip_bases() or ['http://127.0.0.1:8090'])[0][7:]}"
    base = "http://" + h
    try: st = sys_status()
    except Exception: st = {}
    try:
        cn = http.client.HTTPConnection(*TERM_UP, timeout=3); cn.request("GET", "/api/sessions")
        sess = json.loads(cn.getresponse().read()); cn.close()
    except (OSError, ValueError): sess = None
    live = ", ".join(f"`{s['name']}` ({s['id']}, {s['by']})" for s in sess if not s["exited"]) if sess else ""
    mode = "x".join(str(x) for x in ((grabber.mode if grabber else None) or ("?",)))
    ags = [a for a in clip_status()["agents"] if a["online"]]
    return f"""# W105D KVM — hướng dẫn cho agent

Bạn được cấp quyền vào **W105D** (`{socket.gethostname()}`, Armbian trên TV box ZTE W103D/S905X2, eMMC 8GB, địa chỉ `{h}`).
W105D là **KVM-over-IP** cho một **máy đích**: nó giả làm bàn phím/chuột/bút + ổ USB qua cổng USB OTG và thu màn hình
máy đích qua HDMI (card MS2109). Ngoài ra bạn có **terminal root trên chính W105D**.
Người dùng xem mọi thứ bạn làm: màn hình máy đích ở `{base}/`, các phiên terminal ở `{base}/terminal`.

Trạng thái lúc tải trang: capture `{mode}` · USB máy đích `{udc_state()}` · nhiệt độ {st.get('temp', '?')}°C ·
H.264 phần cứng: {'có' if h264.avail() else 'không'} · agent clipboard đang chạy: {', '.join(f"{a['host']} ({a['os']})" for a in ags) or 'không có'} ·
phiên terminal đang mở: {live or 'không có'}.

## 1. Xác thực

Mọi request cần token (người dùng đưa cho bạn; token đổi mỗi lần W105D khởi động lại):

```bash
export XDKVM={h}
export XDKVM_TOKEN=xd-xxxxx-xxxxx-xxxxx-xxxxx
curl -s -H "Authorization: Bearer $XDKVM_TOKEN" http://$XDKVM/health
```

Hoặc thêm `?token=...` vào URL (dùng cho WebSocket). Sai token 10 lần trong 5 phút sẽ bị khoá tạm.

## 2. Điều khiển máy đích (KVM)

| Việc | Lệnh |
|---|---|
| Chụp màn hình | `GET /snap` → JPEG (xem ảnh trước khi thao tác, sau mỗi bước) |
| Gõ chữ | `POST /cmd` body `text Hello world` (ASCII, layout US, ≤200 ký tự/lần, ~45 ms/ký tự) |
| Phím / tổ hợp | `POST /cmd` body `key enter`, `combo ctrl+alt+delete`, `combo win+r` (tên: ctrl shift alt win, a-z 0-9, f1-f12, esc tab enter space backspace delete up down left right home end pageup pagedown printscreen) |
| Chuột theo pixel ảnh /snap | `POST /input` `{{"t":"px","x":640,"y":360,"b":1}}` rồi `{{"t":"px","x":640,"y":360,"b":0}}` (b: 1 trái, 2 phải, 4 giữa) |
| Cuộn | `POST /cmd` body `wheel -3` |
| Nhả mọi phím kẹt | `POST /cmd` body `relall` |
| Giữ phím (khi bật máy) | `POST /input` `{{"t":"kd","u":226}}` … `{{"t":"ku","u":226}}` (HID usage: 224 Ctrl, 225 Shift, 226 Alt, 227 Win) |
| Đèn Caps/Num | `GET /health` → `leds` (bit0 Num, bit1 Caps) |
| Clipboard máy đích | `GET /clip` (text máy đích vừa copy, cần agent clipboard) · `POST /clip` `{{"op":"set","text":"..."}}` |
| BIOS / UEFI / GRUB | Bàn phím của W105D là bàn phím chuẩn boot: `key f2`/`key del` (BIOS), `key f12`/`key f10` (boot menu), `key esc` (GRUB) dùng được khi máy đích đang khởi động; gửi lặp mỗi 0,3 s trong lúc khởi động lại |
| USB cắm vào W105D | `GET /usb` (`ext_dev`, `ext_plugged`, `ext_conf`) · `POST /usb` `{{"op":"ext_off"}}` gỡ khỏi máy đích / `{{"op":"ext_on"}}` gắn lại |
| Sức khoẻ W105D | `GET /sys` · `POST /sys` `{{"op":"replug|capture|hidfwd"}}` · `capture` = reset USB card capture (dùng khi `/health` báo "không trả frame"; không ảnh hưởng máy đích) |
| Macro | `GET /macros` |
| Toàn bộ API | `GET /api` |

Mẹo: gõ lệnh dài trên Windows qua `win+r` → `powershell` → `text …`; tiếng Việt/Unicode thì dùng `POST /clip` set rồi `combo ctrl+v`.
Bàn phím máy đích phải ở kiểu English (tắt Unikey/EVKey) khi gõ bằng `text`.

## 3. Terminal root trên W105D

Mỗi phiên là một bash **root** (hoặc `"user":"sang"`) trên W105D, hiện trực tiếp ở `{base}/terminal`.
Mở **phiên riêng cho mỗi việc**, đặt tên rõ ràng.

CLI (Node 18+ trên máy bạn, tương thích S-remote):

```bash
curl -so ~/xdkvm.js http://$XDKVM/cli/xdkvm.js
node ~/xdkvm.js ls
id=$(node ~/xdkvm.js new --name kiem-tra-usb)
node ~/xdkvm.js run $id -- systemctl is-active kvmweb   # chờ xong, in output, trả exit code
node ~/xdkvm.js send $id journalctl -fu kvmweb          # lệnh chạy lâu: send + wait/tail
node ~/xdkvm.js key $id ctrl-c
node ~/xdkvm.js kill $id
```

HTTP thô:

```
GET    /api/sessions                       danh sách
POST   /api/sessions                       {{"name":"...","cwd":"/root","user":"root"}} → {{"id",...}}
POST   /api/sessions/<id>/input            {{"line":"lệnh"}} (thêm Enter) | {{"data":"\\u0003"}} → {{"offset"}}
GET    /api/sessions/<id>/output?since=<offset>&wait=20000
                                           → {{"data","next","exited","code"}}; đọc tiếp với since=next
DELETE /api/sessions/<id>                  kill
```

Muốn biết exit code: gửi `lệnh; echo "__DONE""_$?"` rồi chờ chuỗi `__DONE_<n>` trong output.
Không chạy chương trình toàn màn hình (vim, less, htop) qua `run`; pager đã tắt (PAGER=cat).
Output là văn bản (bỏ màu), giữ 256 KB cuối mỗi phiên. Phiên mất khi dịch vụ `xd-term` khởi động lại.

## 4. Quyền hạn & quy tắc (bắt buộc)

### 4.1 Máy đích (thiết bị đang cắm vào W105D)

**Trước khi làm bất cứ việc gì trên máy đích**, hỏi người dùng: *"Dữ liệu trên máy đích đã được sao lưu chưa?"*

- **Chưa:** yêu cầu người dùng sao lưu (W105D chỉ có eMMC 8GB, không làm kho sao lưu được; có thể cắm ổ USB vào W105D,
  ổ sẽ chuyển thẳng sang máy đích). Chưa có xác nhận thì chỉ được **xem** (chụp `/snap`, đọc thông tin), không thay đổi gì.
- **Rồi** (người dùng xác nhận): bạn có **toàn quyền** trên máy đích: cài lại hệ điều hành, chia lại phân vùng, đổi
  cài đặt BIOS/UEFI, khởi động lại, cài/gỡ phần mềm, sửa cấu hình… theo yêu cầu công việc.
- Vẫn nên: nói ngắn gọn mình sắp làm gì trước các bước lớn; chụp `/snap` để biết màn hình đang ở đâu trước khi gõ.

### 4.2 W105D (chính thiết bị KVM này)

Bạn có **toàn quyền root** qua `/terminal`: cài gói, sửa file, chạy dịch vụ, debug, khởi động lại dịch vụ… **miễn là
không phá KVM và nhân Linux của W105D.** Cụ thể KHÔNG được:

- Ghi/xoá/format eMMC (`/dev/mmcblk2*`, `boot0/boot1`), sửa `/boot` (u-boot, DTB, uInitrd), kernel, `/lib/modules`.
- Gỡ/hỏng các thành phần KVM: `/home/sang/Run/kvm`, các dịch vụ `usb-kvm-gadget`, `hidfwd`, `kvmweb`, `xd-term`,
  module `amvenc_avc` (H.264), `/usr/local/bin/xd-venc`, Tailscale, S-remote (`sremote`, cổng 2209), mạng WiFi
  (NetworkManager, `net-fallback`) — mất mạng là mất luôn đường vào. Muốn sửa chúng thì hỏi người dùng trước.
- Mount/ghi ổ USB đang chuyển sang máy đích (hai bên cùng ghi = hỏng filesystem): `POST /usb {{"op":"ext_off"}}` trước.
- `apt` gỡ gói hệ thống hoặc `apt full-upgrade` (kernel ophub + module tự build, nâng cấp lung tung có thể hỏng boot/H.264).

Các bẫy kỹ thuật làm treo/hỏng KVM (đã gặp thật):

- Không ghi `/sys/class/usb_role/*/role` (làm chết toàn bộ USB host, card capture chết theo). "Cắm lại USB" an toàn:
  `POST /sys {{"op":"replug"}}`.
- Card capture MS2109 phải cắm cổng USB-A thường (qua hub), cổng OTG (sát nút reset) dành cho cáp tới máy đích.
- WiFi MT7663: đổi AP khi driver đang chạy dễ treo firmware (`mailbox write failed`) → `sudo /usr/local/sbin/wifi-reload`.
- Encoder H.264 (`/dev/amvenc_avc`, `/dev/video*` tên `meson-venc-h264`): không chạy song song nhiều phiên ffmpeg/thử nghiệm
  với tuỳ chọn lạ (`-fflags nobuffer`, `num_capture_buffers`) — từng làm treo cứng box (watchdog reset).
- Không dùng `pkill -f`/`pgrep -f` (dễ giết nhầm phiên terminal); dùng `pgrep -x` hoặc `systemctl`.
- `sync` sau khi ghi file quan trọng (rootfs `data=writeback`: mất điện có thể cắt file vừa ghi).
- Khởi động lại W105D được phép khi cần (KVM mất khoảng 1 phút; token đăng nhập đổi); báo người dùng trước.
- Chỉ đụng phiên terminal do bạn tạo, trừ khi người dùng bảo khác.
"""

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

    def _json(self, d, code=200):
        self._send(code, json.dumps(d, ensure_ascii=False).encode(), "application/json")

    # ---- xac thuc ----
    def _ip(self):
        ip = self.client_address[0]
        if ip.startswith("127.") and self.headers.get("X-Forwarded-For"): ip = self.headers["X-Forwarded-For"].split(",")[0].strip()
        return ip

    def _authed(self):
        """'web' (cookie) | 'agent' (Bearer / ?token=) | None."""
        a = auth_get()
        h = self.headers.get("Authorization", "")
        if h[:7].lower() == "bearer " and hmac.compare_digest(h[7:].strip(), a["token"]): return "agent"
        m = re.search(r"(?:^|;\s*)xdkvm=([0-9a-f]{40})", self.headers.get("Cookie", ""))
        if m and hmac.compare_digest(m.group(1), cookie_val(a)): return "web"
        t = (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("token") or [""])[0]
        if t and hmac.compare_digest(t, a["token"]): return "agent"
        return None

    def _cookie(self, a):
        return {"Set-Cookie": f"xdkvm={cookie_val(a)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"}

    def _gate(self, path):
        """True = duoc di tiep. Chua dang nhap: trang HTML -> chuyen /login, con lai -> 401."""
        if path in PUBLIC or path.startswith("/clip/i/") or path.startswith("/vendor/"): return True
        self.via = self._authed()
        if self.via: return True
        if self.command == "GET" and path in ("/", "/index.html", "/terminal", "/clip/m"):
            nxt = urllib.parse.quote(path if path != "/index.html" else "/")
            self._send(302, b"", extra={"Location": "/login?next=" + nxt}); return False
        if path == "/debug.md":
            self._send(401, ("# W105D KVM: cần token\n\nGửi header `Authorization: Bearer <token>` (hoặc `?token=`).\n"
                             "Token do người dùng claim trên trang chủ sau mỗi lần W105D khởi động; trên box: "
                             "`sudo xd-kvm-token`.\n").encode(), "text/markdown; charset=utf-8")
            return False
        self._json({"error": "cần đăng nhập (token)", "login": "/login"}, 401); return False

    def _auth_post(self, path, raw):
        try: d = json.loads(raw or b"{}")
        except ValueError: d = {}
        if not isinstance(d, dict): d = {}
        ip = self._ip()
        if path == "/auth/claim":
            with _claim_lk:
                a = auth_get()
                if a.get("claimed"): return self._json({"error": "token của lần cắm nguồn này đã được nhận"}, 409)
                a = auth_set(claimed=True, claimed_at=time.time(), claimed_by=ip)
            print(f"auth: token claimed by {ip}", flush=True)
            return self._send(200, json.dumps({"token": a["token"]}).encode(), "application/json", self._cookie(a))
        if path == "/auth/login":
            if too_many(ip): return self._json({"error": "sai quá nhiều lần, thử lại sau 5 phút"}, 429)
            a = auth_get()
            t = str(d.get("token", "")).strip()
            if not t or not hmac.compare_digest(t, a["token"]):
                _fails.setdefault(ip, []).append(time.time()); time.sleep(0.5)
                return self._json({"error": "token không đúng"}, 403)
            if not a.get("claimed"): a = auth_set(claimed=True, claimed_at=time.time(), claimed_by=ip)
            return self._send(200, b'{"ok":true}', "application/json", self._cookie(a))
        if path == "/auth/logout":
            return self._send(200, b'{"ok":true}', "application/json", {"Set-Cookie": "xdkvm=; Path=/; Max-Age=0; SameSite=Lax"})
        if path == "/auth/rotate":
            a = auth_set(token=_tok(), key=secrets.token_hex(16), claimed=True, claimed_at=time.time(), claimed_by=ip)
            print(f"auth: token rotated by {ip}", flush=True)
            return self._send(200, json.dumps({"token": a["token"]}).encode(), "application/json", self._cookie(a))
        self._json({"error": "not found"}, 404)

    # ---- proxy sang xd-term ----
    def _term_proxy(self, raw=None):
        path = self.path
        if self.headers.get("Upgrade", "").lower() == "websocket":
            try: up = socket.create_connection(TERM_UP, timeout=5)
            except OSError: return self._json({"error": "xd-term không chạy"}, 502)
            up.settimeout(None)
            hdr = [f"GET {path} HTTP/1.1"] + [f"{k}: {v}" for k, v in self.headers.items() if k.lower() not in ("cookie", "authorization")]
            up.sendall(("\r\n".join(hdr) + "\r\n\r\n").encode())
            self.close_connection = True
            c = self.connection
            def pipe(a, b):
                try:
                    while True:
                        d = a.recv(65536)
                        if not d: break
                        b.sendall(d)
                except OSError: pass
                for x in (a, b):
                    try: x.shutdown(socket.SHUT_RDWR)
                    except OSError: pass
            t = threading.Thread(target=pipe, args=(up, c), daemon=True); t.start()
            pipe(c, up); t.join(2)
            return
        body = raw
        if self.command == "POST" and path.split("?")[0] == "/api/sessions":
            try:
                d = json.loads(raw or b"{}"); d["by"] = getattr(self, "via", "") or "api"; body = json.dumps(d).encode()
            except (ValueError, TypeError): pass
        try:
            cn = http.client.HTTPConnection(*TERM_UP, timeout=75)
            cn.request(self.command, path, body=body, headers={"Content-Type": "application/json"} if body else {})
            r = cn.getresponse(); b = r.read(); cn.close()
        except OSError as e:
            return self._json({"error": f"xd-term không chạy: {e}"}, 502)
        self._send(r.status, b, r.getheader("Content-Type") or "application/json")

    def _ws_accept(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or "websocket" not in self.headers.get("Upgrade", "").lower():
            self._send(400, b"websocket only"); return False
        acc = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", acc)
        self.end_headers(); self.wfile.flush()
        return True

    def _audio(self):
        c, q = WSConn(self), queue.Queue(maxsize=15)
        def rx():                                   # chi de biet client dong ket noi
            try:
                while c.alive: c.recv()
            except (OSError, EOFError): pass
            c.alive = False
        threading.Thread(target=rx, daemon=True).start()
        audio.add(q)
        try:
            c.text(json.dumps({"t": "a", "rate": AudioHub.RATE, "ch": AudioHub.CH, "fmt": "mulaw"}))
            while c.alive:
                try: b = q.get(timeout=1.0)
                except queue.Empty:
                    if audio.err: c.text(json.dumps({"t": "e", "err": audio.err}))
                    continue
                c.send(2, b)
        except OSError: pass
        finally:
            c.alive = False; audio.remove(q)

    def do_DELETE(self):
        path = self.path.split("?")[0]
        if not self._gate(path): return
        if path.startswith("/api/sessions/"): return self._term_proxy()
        self._send(404, b"not found")

    def _static(self, name, ctype):
        try:
            with open(os.path.join(HERE, name), "rb") as f: body = f.read()
        except OSError: return self._send(404, b"not found")
        self._send(200, body, ctype)

    def do_GET(self):
        path = self.path.split("?")[0]
        if not self._gate(path): return
        if path == "/login":
            if self._authed(): return self._send(302, b"", extra={"Location": "/"})
            return self._static("login.html", "text/html; charset=utf-8")
        if path == "/auth/state":
            a = auth_get()
            return self._json({"claimed": bool(a.get("claimed")), "authed": bool(self._authed())})
        if path.startswith("/vendor/") and re.fullmatch(r"/vendor/[\w.\-]+\.(js|css)", path):
            return self._static(path[1:], "text/css" if path.endswith(".css") else "application/javascript")
        if path == "/cli/xdkvm.js": return self._static("cli/xdkvm.js", "application/javascript; charset=utf-8")
        if path == "/terminal": return self._static("terminal.html", "text/html; charset=utf-8")
        if path == "/debug.md": return self._send(200, debug_md(self.headers.get("Host", "")).encode(), "text/markdown; charset=utf-8")
        if path.startswith("/api/sessions"): return self._term_proxy()
        m = re.fullmatch(r"/clip/i/(\w+)/(agent\.ps1|install\.ps1|agent\.py|install\.sh|m)", path)
        if m and not clip_nonce_ok(m.group(1)): return self._send(403, b"link cai agent het han - bam cai lai tren trang KVM")
        if m: path = "/clip/" + m.group(2)
        if path in ("/", "/index.html"):
            try:
                with open(PAGE_PATH, "rb") as f: body = f.read()
            except OSError as e:
                return self._send(500, f"thiếu {PAGE_PATH}: {e}".encode())
            self._send(200, body, "text/html; charset=utf-8")
        elif path == "/ws":
            if not self._ws_accept(): return
            WSConn(self).serve(grabber)
            self.close_connection = True
        elif path == "/audio":
            if not self._ws_accept(): return
            self._audio()
            self.close_connection = True
        elif path == "/sys":
            self._json(sys_status())
        elif path == "/macros":
            self._json(macros_load())
        elif path == "/clip":
            self._json(clip_status())
        elif path in ("/clip/agent.ps1", "/clip/install.ps1", "/clip/agent.py", "/clip/install.sh", "/clip/m"):
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            b = (qs.get("b") or [""])[0]
            if not re.fullmatch(r"https?://[\w.\-]+(:\d+)?", b): b = ""
            name = path[6:] + (".html" if path == "/clip/m" else "")
            try: body = clip_script(name, b, self.headers.get("Host", ""), m.group(1) if m else "").encode()
            except OSError as e: return self._send(500, str(e).encode())
            self._send(200, body, "text/html; charset=utf-8" if name.endswith(".html") else "text/plain; charset=utf-8")
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
                                        "err": grabber.err, "udc": udc_state(), "modes": grabber.modes, "leds": leds()}).encode(),
                       "application/json")
        elif path == "/usb":
            self._send(200, json.dumps(usb_status()).encode(), "application/json")
        elif path == "/api":
            self._send(200, json.dumps({
                "snap": "GET /snap -> JPEG frame cua man hinh may dich",
                "health": "GET /health -> {mode,fps,kbps,err,udc,modes}",
                "cmd": "POST /cmd body=text|{\"cmd\":...}: key <name> | combo <m+k> | text <s> | "
                       "down/up <k> | move dx dy | click l|r|m | bdown/bup <b> | wheel n | "
                       "abs x y [b] [w] | rel x y [b] [w] | relall",
                "input": "POST /input json: {t:abs,x,y,b,w} | {t:px,x,y,b,w} (pixel /snap) | "
                         "{t:key,s:enter} | {t:combo,s:ctrl+alt+del} | {t:text,s} | {t:kd|ku,u}",
                "usb": "GET /usb -> {ext,ext_conf:auto|off,ext_dev,ext_plugged,names,log} (USB cam vao W105D -> may dich) | "
                       "POST /usb {op:ext_on|ext_off} (bat/tat, giu qua reboot) | {op:rescan|detach}",
                "sys": "GET /sys -> nhiet do, RAM, dia, dich vu, USB, den phim | POST /sys {op:replug|capture|hidfwd}",
                "macros": "GET /macros | POST /macros [{name,script}] (script: text/key/combo/wait/click)",
                "clip": "GET /clip -> clipboard may dich + agent | POST /clip {op:set,text} (dua vao clipboard may dich) | "
                        "{op:install,os:windows|linux|android} (go lenh cai agent) | {op:uninstall,id} | {op:clear}",
                "audio": "WS /audio -> text {t:a,rate,ch,fmt:mulaw} roi khung nhi phan mu-law",
            }, ensure_ascii=False).encode(), "application/json")
        else:
            self._send(404, b"not found")

    def do_POST(self):
        path = self.path.split("?")[0]
        try: n = int(self.headers.get("Content-Length", 0))
        except ValueError: n = 0
        raw = self.rfile.read(min(n, 1 << 20))
        if not self._gate(path): return
        if path.startswith("/auth/"):
            if path == "/auth/rotate" and not self._authed(): return self._json({"error": "cần đăng nhập"}, 401)
            return self._auth_post(path, raw)
        if path.startswith("/api/sessions"): return self._term_proxy(raw)
        if path == "/cmd":   # lenh hidfwd tho - agent goi 1 endpoint la du moi thao tac
            body = raw.decode(errors="replace")
            try:
                d = json.loads(body); body = str(d.get("cmd", ""))
            except (ValueError, AttributeError): pass
            ok = False
            for ln in body.splitlines()[:20]:
                ln = ln.strip()[:200]
                if ln: fwd(ln); ok = True
            return self._send(200 if ok else 400, b"ok" if ok else b"empty")
        if path == "/usb":
            try: d = json.loads(raw or b"{}")
            except ValueError: return self._send(400, b"bad json")
            op = str(d.get("op", ""))
            r = usb_op(op, str(d.get("gib", "")) if op == "resize" else str(d.get("dev", "")), bool(d.get("prealloc")))
            if r is None: return self._send(400, b"bad op: ext_on|ext_off|rescan|detach")
            st = usb_status(); st["result"] = r
            return self._send(200 if r["rc"] == 0 else 500,
                              json.dumps(st, ensure_ascii=False).encode(), "application/json")
        if path in ("/clip/agent", "/clip"):
            try: d = json.loads(raw.decode("utf-8", "replace") or "{}")
            except ValueError: return self._json({"err": "bad json"}, 400)
            if path == "/clip/agent":
                k = self.headers.get("X-Clip-Key", "") or (str(d.get("k", "")) if isinstance(d, dict) else "")
                if not (hmac.compare_digest(k, clip_key()) or self._authed()): return self._json({"err": "sai khoá agent"}, 403)
                ip = self.client_address[0]
                if ip.startswith("127."): ip = (self.headers.get("X-Forwarded-For") or ip).split(",")[0].strip()
                try: return self._json(clip_agent(d, ip))
                except ValueError as e: return self._json({"err": str(e)}, 400)
            op = str(d.get("op", "")) if isinstance(d, dict) else ""
            if op == "set": err = clip_set(d.get("text"))
            elif op == "install": err = clip_install(str(d.get("os", "")), self.headers.get("Host", ""))
            elif op == "uninstall": err = clip_uninstall(str(d.get("id", "")))
            elif op == "clear":
                with clip_lk: HIST.clear()
                err = None
            else: return self._json({"err": "op: set|install|uninstall|clear"}, 400)
            st = clip_status(); st["err"] = err
            return self._json(st, 400 if err else 200)
        if path in ("/sys", "/macros"):
            try: d = json.loads(raw or b"{}")
            except ValueError: return self._json({"err": "bad json"}, 400)
            if path == "/macros":
                try: return self._json(macros_save(d))
                except (ValueError, AttributeError, OSError) as e: return self._json({"err": str(e)}, 400)
            op = str(d.get("op", "")) if isinstance(d, dict) else ""
            if path == "/sys":
                r = sys_op(op)
                if r is None: return self._json({"err": "op: replug|capture|hidfwd"}, 400)
                return self._json(r, 200 if r["rc"] == 0 else 500)
            return self._send(404)
        if path != "/input": return self._send(404)
        try:
            d = json.loads(raw or b"{}")
            t = d.get("t")
            if t == "abs": handle_input(f"m {d.get('x',0)} {d.get('y',0)} {d.get('b',0)} {d.get('w',0)}")
            elif t == "px":   # toa do pixel tren frame /snap -> chuot tuyet doi
                W, H = max(1, int(grabber.mode[0])), max(1, int(grabber.mode[1]))
                x = _i(str(int(d.get("x", 0) * 32767 / W)), 0, 32767)
                y = _i(str(int(d.get("y", 0) * 32767 / H)), 0, 32767)
                handle_input(f"m {x} {y} {_i(str(d.get('b',0)),0,7)} {_i(str(d.get('w',0)),-127,127)}")
            elif t == "key":  # phim theo ten: {"t":"key","s":"enter"}
                s = str(d.get("s", ""))
                if not re.fullmatch(r"[\w+]{1,32}", s): raise ValueError("bad key")
                fwd("key " + s)
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
