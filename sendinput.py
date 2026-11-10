#!/usr/bin/env python3
"""
sendinput.py - client chay tren MAY DIEU KHIEN (Linux).

Bat chuot/ban phim cua may nay, stream qua UDP toi ZTE KVM (hidfwd.py),
ZTE phat lai thanh HID that tren may bi dieu khien. Bam phim chuyen
(mac dinh ScrollLock) de bat/tat - giong nut chuyen cua KVM switch.

Chay:   python3 sendinput.py <ip-zte> [--port 5115] [--key scrolllock|f12|pause]
Vi du:  python3 sendinput.py 192.168.50.149

Yeu cau: user thuoc group 'input' (hoac chay sudo).
"""
import os, sys, socket, struct, selectors, fcntl, glob, time, signal

EV_KEY, EV_REL, EV_SYN = 0x01, 0x02, 0x00
EVIOCGRAB = 0x40044590
INPUT_EVENT = struct.Struct("llHHi")
TOGGLES = {"scrolllock": 70, "f12": 88, "pause": 119}

def _ioc(d, t, n, s): return (d << 30) | (s << 16) | (t << 8) | n
def evbits(fd, ev):
    buf = bytearray(96)
    fcntl.ioctl(fd, _ioc(2, 0x45, 0x20 + ev, 96), buf, True)
    return int.from_bytes(buf, "little")
def is_kbd(fd):
    k = evbits(fd, EV_KEY); return (k >> 30) & 1 and (k >> 28) & 1
def is_mouse(fd):
    return (evbits(fd, EV_REL) >> 0) & 1 and (evbits(fd, EV_KEY) >> 0x110) & 1
def devname(fd):
    buf = bytearray(256)
    try:
        fcntl.ioctl(fd, _ioc(2, 0x45, 0x06, 256), buf, True)
        return buf.split(b"\0")[0].decode(errors="replace")
    except OSError: return "?"

def main():
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        print(__doc__); return
    host, port, tkey = args.pop(0), 5115, "scrolllock"
    while args:
        a = args.pop(0)
        if a == "--port": port = int(args.pop(0))
        elif a == "--key": tkey = args.pop(0).lower()
    tcode = TOGGLES.get(tkey, 70)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    def send(line): sock.sendto(line.encode(), (host, port))

    sel = selectors.DefaultSelector()
    fds = {}
    def add(path):
        try: fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError: return
        try:
            if is_kbd(fd) or is_mouse(fd):
                sel.register(fd, selectors.EVENT_READ)
                fds[fd] = devname(fd)
                print("  theo doi:", path, "-", fds[fd])
            else: os.close(fd)
        except OSError: os.close(fd)

    print("Quet thiet bi input...")
    for p in glob.glob("/dev/input/event*"): add(p)
    seen = set(glob.glob("/dev/input/event*"))
    if not fds:
        print("Khong tim thay chuot/phim. Kiem tra quyen /dev/input (group input).")
        return

    active = False
    def set_active(on):
        nonlocal active
        active = on
        for fd in fds:
            try: fcntl.ioctl(fd, EVIOCGRAB, 1 if on else 0)
            except OSError: pass
        if not on: send("relall")
        print(f"[{'>> MAY BI DIEU KHIEN <<' if on else 'may nay'}]  (bam {tkey} de chuyen)")

    print(f"\nSan sang. Bam [{tkey.upper()}] de chuyen dieu khien sang may dich ({host}).")
    def bye(*_):
        for fd in fds:
            try: fcntl.ioctl(fd, EVIOCGRAB, 0)
            except OSError: pass
        send("relall"); sys.exit(0)
    signal.signal(signal.SIGINT, bye); signal.signal(signal.SIGTERM, bye)

    t_scan = time.time()
    while True:
        for key, _ in sel.select(timeout=1.0):
            fd = key.fd
            while True:
                try: b = os.read(fd, INPUT_EVENT.size * 8)
                except BlockingIOError: break
                except OSError: b = b""
                if not b:
                    try: sel.unregister(fd); os.close(fd); fds.pop(fd, None)
                    except Exception: pass
                    break
                for off in range(0, len(b) - INPUT_EVENT.size + 1, INPUT_EVENT.size):
                    _, _, t, c, v = INPUT_EVENT.unpack_from(b, off)
                    if t == EV_KEY and c == tcode and v == 1:
                        set_active(not active); continue
                    if not active: continue
                    if t == EV_KEY and v == 2: continue  # bo autorepeat, HID tu lap
                    send(f"ev {t} {c} {v}")
        if time.time() - t_scan > 3:
            t_scan = time.time()
            for p in glob.glob("/dev/input/event*"):
                if p not in seen: seen.add(p); add(p)

if __name__ == "__main__":
    main()
