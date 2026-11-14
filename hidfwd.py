#!/usr/bin/env python3
"""
hidfwd.py - USB HID pass-through + remote control cho ZTE W105D/W103D.

Doc chuot/ban phim that tu /dev/input/event*, phat lai thanh HID gadget
(/dev/hidg0 = keyboard, /dev/hidg1 = mouse) -> may tinh dich cam vao cong
USB-A sat nut reset bang cap duc-duc.

Dong thoi mo UDP port (mac dinh 5115) de dieu khien tu xa qua mang.

Cau hinh gadget truoc:  sudo bash gadget-setup.sh
Chay:                   python3 hidfwd.py [--port 5115] [--no-grab]
Gui lenh remote:        python3 hidfwd.py send "text hello"
                    hoac:  echo "text hello" | nc -u -w0 <ip-box> 5115

Lenh UDP (dang text, moi dong 1 lenh):
  text <chuoi>          go van ban ASCII
  key <ten>             nhan roi nha (vd: key enter, key f5)
  combo <m+k+key>       to hop phim (vd: combo ctrl+alt+del, combo shift+a)
  down <ten> | up <ten> giu / nha phim
  move <dx> <dy>        di chuyen con tro tuong doi
  click <left|right|middle>        nhap chuot
  bdown <btn> | bup <btn>          giu / nha nut chuot (keo tha)
  wheel <n>             cuon (+ len, - xuong)
  rel <dx> <dy> [b] [w] chuot tuong doi kem trang thai nut
  rawk <hex16>          report ban phim tho 8 byte
  rawm <hex8>           report chuot tho 4 byte
  ev <type> <code> <v>  forward 1 evdev event tho (dung cho sendinput.py)
  abs <x> <y> [b] [w]   chuot tuyet doi (x,y: 0..32767, b: nut, w: cuon)
  hdown <u> | hup <u>   nhan/nha phim theo HID usage (>=0xe0 = modifier)
  relall                nha het phim/nut dang giu
"""
import os, sys, socket, struct, selectors, select, fcntl, time, glob, signal

UDP_PORT = 5115
HIDG_K, HIDG_M, HIDG_A = "/dev/hidg0", "/dev/hidg1", "/dev/hidg2"   # ban phim boot, chuot tuong doi, but (Digitizer Pen)
HIDG_B = "/dev/hidg3"                    # chuot tuyet doi (gadget-setup.sh hid.usb3, report 6 byte khong report ID)
ABSMODE_F = "/var/lib/xd-kvm/absmode"    # "mouse" (mac dinh: Windows/Linux/mac) | "pen" (Android khong nhan chuot tuyet doi)
LEDS_F = "/run/xd-hid-leds"              # den ban phim may dich (bit0 Num, bit1 Caps, bit2 Scroll) - kvmweb doc
def absmode_get():
    try:
        with open(ABSMODE_F) as f: v = f.read().strip()
        return v if v in ("mouse", "pen") else "mouse"
    except OSError: return "mouse"

EV_KEY, EV_REL, EV_SYN = 0x01, 0x02, 0x00
REL_X, REL_Y, REL_WHEEL, REL_HWHEEL = 0, 1, 11, 6
EVIOCGRAB = 0x40044590
INPUT_EVENT = struct.Struct("llHHi")  # 24 bytes

def _ioc(dir_, type_, nr, size):
    return (dir_ << 30) | (size << 16) | (type_ << 8) | nr
def EVIOCGBIT(ev, size=96):
    return _ioc(2, 0x45, 0x20 + ev, size)

def evbits(fd, ev):
    buf = bytearray(96)
    fcntl.ioctl(fd, EVIOCGBIT(ev), buf, True)
    return int.from_bytes(buf, "little")

# ---------------- evdev -> HID mapping ----------------
# evdev code -> HID usage (page 0x07)
KMAP = {
    30:0x04,48:0x05,46:0x06,32:0x07,18:0x08,33:0x09,34:0x0a,35:0x0b,23:0x0c,
    36:0x0d,37:0x0e,38:0x0f,50:0x10,49:0x11,24:0x12,25:0x13,16:0x14,19:0x15,
    31:0x16,20:0x17,22:0x18,47:0x19,17:0x1a,45:0x1b,21:0x1c,44:0x1d,
    2:0x1e,3:0x1f,4:0x20,5:0x21,6:0x22,7:0x23,8:0x24,9:0x25,10:0x26,11:0x27,
    28:0x28,1:0x29,14:0x2a,15:0x2b,57:0x2c,12:0x2d,13:0x2e,26:0x2f,27:0x30,
    43:0x31,39:0x33,40:0x34,41:0x35,51:0x36,52:0x37,53:0x38,58:0x39,
    59:0x3a,60:0x3b,61:0x3c,62:0x3d,63:0x3e,64:0x3f,65:0x40,66:0x41,67:0x42,
    68:0x43,87:0x44,88:0x45,99:0x46,70:0x47,119:0x48,
    110:0x49,102:0x4a,104:0x4b,111:0x4c,107:0x4d,109:0x4e,
    106:0x4f,105:0x50,108:0x51,103:0x52,69:0x53,
    98:0x54,55:0x55,74:0x56,78:0x57,96:0x58,
    79:0x59,80:0x5a,81:0x5b,75:0x5c,76:0x5d,77:0x5e,71:0x5f,72:0x60,73:0x61,
    82:0x62,83:0x63,
    86:0x64,127:0x65,139:0x65,116:0x66,117:0x67,
    183:0x68,184:0x69,185:0x6a,186:0x6b,187:0x6c,188:0x6d,189:0x6e,190:0x6f,
    191:0x70,192:0x71,193:0x72,194:0x73,
}
MODMAP = {  # evdev -> modifier bit
    29:0x01, 42:0x02, 56:0x04, 125:0x08,
    97:0x10, 54:0x20, 100:0x40, 126:0x80,
}
BTNMAP = {0x110:0x01, 0x111:0x02, 0x112:0x04}  # BTN_LEFT/RIGHT/MIDDLE

# ten phim -> HID usage (cho lenh UDP)
KEYNAME = {
    "a":0x04,"b":0x05,"c":0x06,"d":0x07,"e":0x08,"f":0x09,"g":0x0a,"h":0x0b,
    "i":0x0c,"j":0x0d,"k":0x0e,"l":0x0f,"m":0x10,"n":0x11,"o":0x12,"p":0x13,
    "q":0x14,"r":0x15,"s":0x16,"t":0x17,"u":0x18,"v":0x19,"w":0x1a,"x":0x1b,
    "y":0x1c,"z":0x1d,
    "1":0x1e,"2":0x1f,"3":0x20,"4":0x21,"5":0x22,"6":0x23,"7":0x24,"8":0x25,
    "9":0x26,"0":0x27,
    "enter":0x28,"esc":0x29,"escape":0x29,"backspace":0x2a,"tab":0x2b,
    "space":0x2c,"minus":0x2d,"-":0x2d,"equal":0x2e,"=":0x2e,
    "[":0x2f,"]":0x30,"\\":0x31,";":0x33,"'":0x34,"`":0x35,",":0x36,".":0x37,
    "/":0x38,"capslock":0x39,
    "f1":0x3a,"f2":0x3b,"f3":0x3c,"f4":0x3d,"f5":0x3e,"f6":0x3f,"f7":0x40,
    "f8":0x41,"f9":0x42,"f10":0x43,"f11":0x44,"f12":0x45,
    "printscreen":0x46,"scrolllock":0x47,"pause":0x48,
    "insert":0x49,"home":0x4a,"pageup":0x4b,"del":0x4c,"delete":0x4c,
    "end":0x4d,"pagedown":0x4e,"right":0x4f,"left":0x50,"down":0x51,"up":0x52,
    "numlock":0x53,"menu":0x65,"power":0x66,"compose":0x65,"app":0x65,
}
MODNAME = {"ctrl":0x01,"lctrl":0x01,"shift":0x02,"lshift":0x02,"alt":0x04,
           "lalt":0x04,"win":0x08,"meta":0x08,"lwin":0x08,"super":0x08,
           "rctrl":0x10,"rshift":0x20,"ralt":0x40,"rwin":0x80,"rmeta":0x80}

# ASCII -> (shift?, usage)
CHARMAP = {}
for ch, u in zip("abcdefghijklmnopqrstuvwxyz", range(0x04, 0x1e)): CHARMAP[ch] = (0, u)
for ch, u in zip("1234567890", range(0x1e, 0x28)): CHARMAP[ch] = (0, u)
CHARMAP.update({" ":(0,0x2c),"\n":(0,0x28),"\t":(0,0x2b),"-":(0,0x2d),"=":(0,0x2e),
    "[":(0,0x2f),"]":(0,0x30),"\\":(0,0x31),";":(0,0x33),"'":(0,0x34),"`":(0,0x35),
    ",":(0,0x36),".":(0,0x37),"/":(0,0x38)})
for ch, base in zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"):
    CHARMAP[ch] = (1, CHARMAP[base][1])
CHARMAP.update({"!":(1,0x1e),"@":(1,0x1f),"#":(1,0x20),"$":(1,0x21),"%":(1,0x22),
    "^":(1,0x23),"&":(1,0x24),"*":(1,0x25),"(":(1,0x26),")":(1,0x27),
    "_":(1,0x2d),"+":(1,0x2e),"{":(1,0x2f),"}":(1,0x30),"|":(1,0x31),
    ":":(1,0x33),'"':(1,0x34),"~":(1,0x35),"<":(1,0x36),">":(1,0x37),"?":(1,0x38)})

# ---------------- HID emit ----------------
class Hid:
    def __init__(self):
        self.kfd = self.mfd = self.afd = self.bfd = -1
        self.absmode = absmode_get()
        self.reopen()
    def reopen(self):
        for attr, path in (("kfd", HIDG_K), ("mfd", HIDG_M), ("afd", HIDG_A), ("bfd", HIDG_B)):
            if getattr(self, attr) < 0:
                try: setattr(self, attr, os.open(path, os.O_WRONLY | os.O_NONBLOCK))
                except OSError: pass
    def _w(self, fd, data):
        if fd < 0: return
        try:
            try: os.write(fd, data)
            except BlockingIOError:
                select.select([], [fd], [], 0.03)
                try: os.write(fd, data)
                except BlockingIOError: pass
        except OSError:
            try: os.close(fd)
            except OSError: pass
            if fd == self.kfd: self.kfd = -1
            elif fd == self.mfd: self.mfd = -1
            elif fd == self.bfd: self.bfd = -1
            else: self.afd = -1
            self.reopen()
    def kbd(self, mods, keys):
        rep = bytes([mods, 0] + list(keys)[:6] + [0] * (6 - len(keys)))
        self._w(self.kfd, rep)
    def mouse(self, btns, dx=0, dy=0, wh=0):
        if not (dx or dy or wh):
            self._w(self.mfd, struct.pack("Bbbb", btns, 0, 0, 0))
            return
        while dx or dy or wh:
            x, y, w = (max(-127, min(127, v)) for v in (dx, dy, wh))
            self._w(self.mfd, struct.pack("Bbbb", btns, x, y, w))
            dx -= x; dy -= y; wh -= w
    ax = ay = 16384; abtn = 0
    def set_absmode(self, m):
        if m not in ("mouse", "pen") or m == self.absmode: return
        if self.absmode == "pen":   # but roi khoi vung -> Windows thoi coi la but
            self._w(self.afd, struct.pack("<BHH", 0, self.ax, self.ay))
        self.absmode = m
        try:
            os.makedirs(os.path.dirname(ABSMODE_F), exist_ok=True)
            with open(ABSMODE_F + ".tmp", "w") as f: f.write(m + "\n")
            os.replace(ABSMODE_F + ".tmp", ABSMODE_F)
        except OSError: pass
    def amouse(self, x, y, btns=0, wh=0):  # absolute pointer: x,y 0..32767
        self.ax, self.ay, self.abtn = x, y, btns & 7
        if self.absmode == "mouse" and self.bfd >= 0:
            # chuot tuyet doi that (report ID 4): con tro chuot binh thuong, hover + keo chon text dung
            self._w(self.bfd, struct.pack("<BHHb", btns & 7, max(0, min(32767, x)), max(0, min(32767, y)),
                                          max(-127, min(127, wh))))
            return
        # hidg2 = Digitizer Pen: b0=Tip, b1=Barrel, b2=InRange (luon 1)
        # Windows: chuot phai = Barrel + Tip cung luc (Barrel khi dang hover bi bo qua)
        bits = (1 if btns & 3 else 0) | (2 if btns & 2 else 0) | 4
        self._w(self.afd, struct.pack("<BHH", bits, max(0, min(32767, x)), max(0, min(32767, y))))
        if wh: self.mouse(0, 0, 0, wh)  # Pen khong co banh xe -> cuon qua chuot tuong doi

class KbdState:
    def __init__(self, hid): self.hid, self.mods, self.keys = hid, 0, set()
    def update(self):
        self.hid.kbd(self.mods, [u for u in self.keys if u])
    def evdev(self, code, val):
        if code in MODMAP:
            self.mods = (self.mods | MODMAP[code]) if val else (self.mods & ~MODMAP[code])
        elif code in KMAP:
            u = KMAP[code]
            (self.keys.add(u) if val else self.keys.discard(u))
        else:
            return
        self.update()
    def tap(self, mods, u, hold=0.02):
        self.mods |= mods
        if u: self.keys.add(u)
        self.update()
        time.sleep(hold)
        if u: self.keys.discard(u)
        self.mods &= ~mods; self.update()
        time.sleep(hold)
    def raw(self, mods, keys):
        self.hid.kbd(mods, keys)

class MouseState:
    def __init__(self, hid): self.hid, self.btns, self.dx, self.dy, self.wh = hid, 0, 0, 0, 0
    def flush(self):
        if self.dx or self.dy or self.wh:
            self.hid.mouse(self.btns, self.dx, self.dy, self.wh)
            self.dx = self.dy = self.wh = 0
        else:
            self.hid.mouse(self.btns)
    def btn(self, code, val):
        if code in BTNMAP:
            b = BTNMAP[code]
            self.btns = (self.btns | b) if val else (self.btns & ~b)
            self.hid.mouse(self.btns)
    def rel(self, code, val):
        if code == REL_X: self.dx += val
        elif code == REL_Y: self.dy += val
        elif code == REL_WHEEL: self.wh += val

def caps_on():
    """Den Caps Lock cua may dich (led_reader ghi /run/xd-hid-leds; bit 1 = Caps)."""
    try:
        with open(LEDS_F) as f: return bool(int(f.read().strip() or 0) & 2)
    except (OSError, ValueError): return False

# ---------------- UDP command parser ----------------
def handle_cmd(line, kb, ms):
    line = line.lstrip().rstrip("\r\n")   # giu nguyen dau cach trong 'text'
    cmd, _, arg = line.partition(" ")
    cmd = cmd.strip().lower()
    if not cmd: return
    if cmd == "text":
        caps = caps_on()
        for ch in arg:
            m = CHARMAP.get(ch)
            if m:
                sh = m[0] ^ (1 if caps and ch.isalpha() else 0)   # Caps Lock dang bat tren may dich -> dao Shift cho chu cai
                kb.tap(0x02 if sh else 0, m[1])
    elif cmd == "key":
        t = arg.strip().lower().split()
        for name in t:
            if name in KEYNAME: kb.tap(0, KEYNAME[name])
            elif name in MODNAME: kb.tap(MODNAME[name], 0)
    elif cmd == "combo":
        mods, u = 0, 0
        for part in arg.strip().lower().split("+"):
            if part in MODNAME: mods |= MODNAME[part]
            elif part in KEYNAME: u = KEYNAME[part]
        kb.tap(mods, u, hold=0.05)
    elif cmd in ("down", "up"):
        u = KEYNAME.get(arg.strip().lower())
        if u: (kb.keys.add(u) if cmd == "down" else kb.keys.discard(u)); kb.update()
        m = MODNAME.get(arg.strip().lower())
        if m: kb.mods = (kb.mods | m) if cmd == "down" else (kb.mods & ~m); kb.update()
    elif cmd == "move":
        a = arg.split(); ms.hid.mouse(ms.btns, int(a[0]), int(a[1]))
    elif cmd == "click":
        b = {"left":1, "right":2, "middle":4}.get(arg.strip().lower(), 1)
        ms.hid.mouse(ms.btns | b); time.sleep(0.03); ms.hid.mouse(ms.btns)
    elif cmd == "bdown":
        ms.btns |= {"left":1, "right":2, "middle":4}.get(arg.strip().lower(), 1)
        ms.hid.mouse(ms.btns)
    elif cmd == "bup":
        ms.btns &= ~{"left":1, "right":2, "middle":4}.get(arg.strip().lower(), 1)
        ms.hid.mouse(ms.btns)
    elif cmd == "wheel":
        ms.hid.mouse(ms.btns, 0, 0, int(arg.strip() or 0))
    elif cmd == "ev":  # ev <type> <code> <value> - forward evdev event tho
        a = arg.split(); t, c, v = int(a[0]), int(a[1]), int(a[2])
        if t == EV_KEY: kb.evdev(c, v); ms.btn(c, v)
        elif t == EV_REL: ms.rel(c, v)
        elif t == EV_SYN: ms.flush()
    elif cmd == "abs":  # abs <x> <y> <btns> <wheel> - chuot tuyet doi (0..32767)
        a = arg.split(); ms.hid.amouse(int(a[0]), int(a[1]), int(a[2]) if len(a) > 2 else 0, int(a[3]) if len(a) > 3 else 0)
    elif cmd == "rel":  # rel <dx> <dy> <btns> <wheel> - chuot tuong doi (web pointer-lock)
        a = arg.split(); ms.btns = int(a[2]) & 7 if len(a) > 2 else ms.btns
        ms.hid.mouse(ms.btns, int(a[0]), int(a[1]), int(a[3]) if len(a) > 3 else 0)
    elif cmd in ("hdown", "hup"):  # hdown/hup <usage> - raw HID usage (>=0xe0 la modifier)
        u = int(arg.strip(), 0)
        if u >= 0xE0:
            b = 1 << (u - 0xE0)
            kb.mods = (kb.mods | b) if cmd == "hdown" else (kb.mods & ~b)
        else:
            (kb.keys.add(u) if cmd == "hdown" else kb.keys.discard(u))
        kb.update()
    elif cmd == "absmode":  # absmode mouse|pen
        ms.hid.set_absmode(arg.strip().lower())
    elif cmd == "relall":  # nha het phim + nut chuot
        kb.keys.clear(); kb.mods = 0; kb.update(); ms.btns = 0; ms.hid.mouse(0)
        if ms.hid.abtn: ms.hid.amouse(ms.hid.ax, ms.hid.ay, 0, 0)
    elif cmd == "rawk":
        r = bytes.fromhex(arg.replace(" ", "")); kb.hid.kbd(r[0], r[2:8])
    elif cmd == "rawm":
        r = bytes.fromhex(arg.replace(" ", "")); ms.hid.mouse(r[0], r[1], r[2], r[3])

# ---------------- den ban phim (output report cua host) ----------------
def led_reader():
    """Host gui output report 1 byte (Num/Caps/Scroll) toi hid.usb0 -> doc tu /dev/hidg0 bang fd rieng."""
    import threading
    def run():
        last = None
        while True:
            try: fd = os.open(HIDG_K, os.O_RDONLY)
            except OSError: time.sleep(2); continue
            try:
                while True:
                    b = os.read(fd, 8)
                    if not b: break
                    v = b[0]
                    if v != last:
                        last = v
                        try:
                            with open(LEDS_F + ".tmp", "w") as f: f.write(str(v))
                            os.replace(LEDS_F + ".tmp", LEDS_F)
                        except OSError: pass
            except OSError: pass
            finally:
                os.close(fd)
            time.sleep(1)
    threading.Thread(target=run, daemon=True).start()

# ---------------- main loop ----------------
def is_kbd(fd):
    k = evbits(fd, EV_KEY)
    return (k >> 30) & 1 and (k >> 28) & 1  # KEY_A(30) va KEY_ENTER(28)
def is_mouse(fd):
    return (evbits(fd, EV_REL) >> REL_X) & 1 and (evbits(fd, EV_KEY) >> 0x110) & 1

def main():
    port, grab, local = UDP_PORT, True, True
    args = sys.argv[1:]
    if args and args[0] == "send":
        if "--port" in args:
            i = args.index("--port"); port = int(args[i + 1]); del args[i:i + 2]
        line = " ".join(args[1:])
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(line.encode(), ("127.0.0.1", port))
        print("sent:", line); return
    while args:
        a = args.pop(0)
        if a == "--port": port = int(args.pop(0))
        elif a == "--no-grab": grab = False
        elif a == "--no-local": local = False   # khong chuyen tiep ban phim/chuot cam vao box

    hid = Hid(); kb = KbdState(hid); ms = MouseState(hid)
    led_reader()
    sel = selectors.DefaultSelector()

    def add_dev(path):
        if not local: return
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError: return
        try:
            if is_kbd(fd) or is_mouse(fd):
                if grab:
                    try: fcntl.ioctl(fd, EVIOCGRAB, 1)
                    except OSError: pass
                sel.register(fd, selectors.EVENT_READ, path)
                print("forward:", path)
            else:
                os.close(fd)
        except OSError:
            os.close(fd)

    for p in glob.glob("/dev/input/event*"): add_dev(p)
    seen = set(glob.glob("/dev/input/event*"))

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("0.0.0.0", port)); udp.setblocking(False)
    sel.register(udp, selectors.EVENT_READ, "udp")
    print(f"UDP remote port {port} - san sang. Ctrl+C de thoat.")

    def cleanup(*_):
        for fd in list(sel.get_map()):
            if isinstance(fd, int):
                try: fcntl.ioctl(fd, EVIOCGRAB, 0)
                except OSError: pass
        kb.raw(0, []); ms.hid.mouse(0)  # nha het phim/nut
        sys.exit(0)
    signal.signal(signal.SIGINT, cleanup); signal.signal(signal.SIGTERM, cleanup)

    t_scan = time.time()
    while True:
        for key, _ in sel.select(timeout=1.0):
            if key.data == "udp":
                while True:
                    try: pkt = udp.recv(4096)
                    except (BlockingIOError, InterruptedError): break
                    try: handle_cmd(pkt.decode(errors="replace"), kb, ms)
                    except Exception: pass
            else:
                fd = key.fd
                while True:
                    try: b = os.read(fd, INPUT_EVENT.size * 8)
                    except BlockingIOError: break
                    except OSError: b = b""
                    if not b:
                        try: sel.unregister(fd); os.close(fd)
                        except Exception: pass
                        break
                    for off in range(0, len(b) - INPUT_EVENT.size + 1, INPUT_EVENT.size):
                        _, _, t, c, v = INPUT_EVENT.unpack_from(b, off)
                        if t == EV_KEY:
                            kb.evdev(c, v); ms.btn(c, v)
                        elif t == EV_REL:
                            ms.rel(c, v)
                        elif t == EV_SYN:
                            ms.flush()
        if time.time() - t_scan > 2:  # quet thiet bi moi cam nong
            t_scan = time.time()
            hid.reopen()
            for p in glob.glob("/dev/input/event*"):
                if p not in seen: seen.add(p); add_dev(p)

if __name__ == "__main__":
    main()
