# kvm-zte

Web KVM over IP chạy trên box ZTE W103D/W105D (Armbian ophub, S905X2). Giao diện và tính năng port từ KVM của W152D
(trừ ổ USB ảo / ISO ảo / bản gốc rootfs, vì W105D chỉ có eMMC 8GB).

- **Hình**: card HDMI capture USB MS2109 (MJPEG). Gửi qua WebSocket theo 2 kiểu:
  - **H.264 phần cứng** (mặc định): `xd-venc` giải nén MJPEG (libjpeg-turbo) → HCodec của S905X2 (module `amvenc_avc`,
    xem `tools/amvenc/`) → trình duyệt giải mã bằng WebCodecs (https) hoặc MSE (http). 720p30 ≈ 3 Mbit/s, 1080p30 ≈ 6 Mbit/s,
    trễ trên box 18 ms (720p) / 33 ms (1080p).
  - **MJPEG delta**: chỉ gửi các đoạn restart-interval thay đổi; flow-control theo ack.
- **Chuột/phím**: USB gadget (configfs HID) trên cổng OTG: bàn phím chuẩn boot (BIOS/UEFI/GRUB nhận), chuột tương đối,
  bút (Digitizer Pen, cho Android) và chuột tuyệt đối thật (Windows/Linux/macOS). Đèn Caps/Num của máy đích hiện trên trang.
- **USB lưu trữ**: USB cắm vào cổng host của box → udev gán `/dev/sdX` vào LUN `mass_storage` → máy đích thấy như USB cắm
  trực tiếp. Bật/tắt trên trang (tab Lưu trữ), giữ qua reboot.
- **Khác**: âm thanh HDMI, macro, clipboard 2 chiều (agent trên máy đích), sức khoẻ box, reset card capture, terminal root
  cho agent (`/terminal`, `/api/sessions`), hướng dẫn cho AI agent (`/debug.md`), CLI `cli/xdkvm.js`.
- **Xác thực**: token mới mỗi lần box khởi động (`/run/xd-kvm/auth.json`). Người đầu tiên bấm **Claim** trên trang nhận
  token; trên box: `sudo xd-kvm-token` (in token) / `sudo xd-kvm-token reset`. Agent dùng `Authorization: Bearer <token>`.

## Thành phần

| File | Vai trò |
|---|---|
| `gadget-setup.sh` | Tạo gadget: `hidg0` phím boot, `hidg1` chuột tương đối, `hidg2` bút, `hidg3` chuột tuyệt đối, `mass_storage.usb0`; chuyển cổng OTG sang DWC2 |
| `hidfwd.py` | Ghi report HID; lệnh UDP `127.0.0.1:5115`; chuyển tiếp chuột/phím cắm vào box; đọc đèn bàn phím → `/run/xd-hid-leds` |
| `kvmweb.py` | HTTP/WebSocket :8090 (chạy root): capture V4L2, H.264 hub (`xd-venc`), xác thực, API, âm thanh, clipboard, macro, proxy terminal |
| `kvm.html`, `login.html`, `terminal.html` | Giao diện (desktop + điện thoại) |
| `xdterm.py` | PTY session host 127.0.0.1:8092 (dịch vụ `xd-term`), API giống S-remote |
| `tools/xd-venc/xd-venc.c` | MJPEG → H.264 qua `/dev/amvenc_avc` (giao thức pipe giống xd-venc của W152D) |
| `tools/amvenc/` | `build.sh` + `g12a.patch`: build module encoder cho G12A từ meson-sm1-codecs |
| `usb-share.sh`, `99-kvm-usb-share.rules` | Gán/rút USB vào LUN; `on`/`off` (cờ `/var/lib/xd-kvm/usb-share.off`) |
| `sbin/xd-kvm-token` | Xem/đổi token (cài vào `/usr/local/sbin`) |
| `clip/` | Agent clipboard (Windows `.ps1`, Linux `.py`, trang Android `m.html`) |
| `vendor/` | xterm.js + addon-fit (MIT) cho `/terminal` |
| `sendinput.py` | Client Linux: dùng chuột/phím máy điều khiển qua UDP |
| `*.service` | Unit systemd (`usb-kvm-gadget`, `hidfwd`, `kvmweb`, `xd-term`) |

## Cài đặt (box đã có Armbian ophub, user `sang`)

```bash
sudo apt install -y python3 alsa-utils libturbojpeg0-dev gcc make git
git clone <repo> /home/sang/Run/kvm && cd /home/sang/Run/kvm
sudo cp usb-kvm-gadget.service hidfwd.service kvmweb.service xd-term.service /etc/systemd/system/
sudo cp 99-kvm-usb-share.rules /etc/udev/rules.d/
sudo install -m755 sbin/xd-kvm-token /usr/local/sbin/
sudo tools/amvenc/build.sh            # H.264 phần cứng (tuỳ chọn; không có thì chỉ MJPEG)
sudo systemctl daemon-reload && sudo udevadm control --reload
sudo systemctl enable --now usb-kvm-gadget hidfwd kvmweb xd-term
```

**Cổng USB:** cổng OTG (USB-A ngoài cùng bên trái, sát nút reset) → cáp USB-A đực-đực tới máy đích. Card MS2109 **phải cắm
cổng USB-A thường** (hub onboard), không cắm cổng OTG. Không ghi `/sys/class/usb_role/*/role` (làm chết USB host).

Mở `http://<ip-box>:8090`. Độ phân giải đã chọn lưu ở `kvmweb.json`. Đường dẫn trong unit là `/home/sang/Run/kvm`.

## Ghi chú

- Phần cứng mã hoá tối đa 1920×1088: các chế độ 1600×1200 của card chỉ dùng được ở MJPEG.
- Đổi kernel → chạy lại `tools/amvenc/build.sh` (module không nạp được thì trang tự dùng MJPEG).
- Qua Tailscale relay (DERP) băng thông chỉ ~12–16 Mbit/s: dùng H.264; MJPEG với nội dung động cần ~40 Mbit/s.
- Không chạy song song thử nghiệm ffmpeg `h264_v4l2m2m` với tuỳ chọn lạ (`-fflags nobuffer`, `num_capture_buffers`) trên
  encoder: từng làm treo cứng box.
