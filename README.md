# kvm-zte

Web KVM over IP chạy trên box ZTE W103D/W105D (Armbian):

- **Hình**: lấy màn hình máy đích qua HDMI capture USB (MacroSilicon MS2109, MJPEG), stream qua WebSocket với flow-control theo ack → độ trễ thấp, fps tự thích nghi theo mạng.
- **Chuột/phím**: ZTE giả lập bàn phím + chuột (tương đối và tuyệt đối) qua USB gadget (configfs HID) trên cổng OTG nối với máy đích.
- **USB lưu trữ**: cắm USB vào cổng host của ZTE → udev gán `/dev/sdX` vào LUN `mass_storage` của gadget → máy đích thấy như USB cắm trực tiếp (đọc/ghi được). Rút ra là eject mềm, cắm stick khác là tự gán tiếp.
- **Giao diện web** (`:8090`): máy tính và điện thoại (touchpad/chạm trực tiếp, pinch zoom, bàn phím ảo + hàng phím phụ, chọn độ phân giải/fps).

## Thành phần

| File | Vai trò |
|---|---|
| `gadget-setup.sh` | Tạo USB HID gadget (`/dev/hidg0` phím, `hidg1` chuột tương đối, `hidg2` chuột tuyệt đối), chuyển cổng OTG sang device |
| `hidfwd.py` | Ghi report HID; nhận lệnh UDP `127.0.0.1:5115`; chuyển tiếp chuột/phím cắm trực tiếp vào ZTE |
| `kvmweb.py` | HTTP + WebSocket server: capture V4L2 (không cần ffmpeg), `/ws`, `/stream`, `/snap`, `/health` |
| `kvm.html` | Giao diện web |
| `sendinput.py` | Client Linux: dùng chuột/phím của máy điều khiển qua UDP (phím ScrollLock để chuyển) |
| `usb-share.sh` | Gán/rút USB lưu trữ vào LUN `mass_storage` (umount trên ZTE trước, log ở `/run/kvm-usb-share.log`) |
| `99-kvm-usb-share.rules` | Rule udev: tự gọi `usb-share.sh` khi cắm/rút disk USB |
| `*.service` | Unit systemd |

## HTTP API (`:8090`, cho script/AI agent)

- `GET /snap` 1 frame JPEG · `GET /stream` MJPEG · `GET /health` trạng thái · `GET /api` mô tả endpoint
- `POST /cmd` — lệnh hidfwd thô (text hoặc `{"cmd":...}`): `key enter`, `combo ctrl+alt+del`, `text <s>`, `abs x y b`, `relall`, ...
- `POST /input` — JSON: `{t:abs|px,x,y,b,w}` (`px` = toạ độ pixel của `/snap`, tự quy đổi), `{t:key,s:enter}`, `{t:combo|text,s}`, `{t:kd|ku,u}`
- `GET /usb` — trạng thái USB passthrough · `POST /usb` — `{op:detach|rescan|attach|eject, dev?}` (gọi `sudo usb-share.sh`, `detach` = eject mềm không gán lại)

## Cài đặt

```bash
sudo cp usb-kvm-gadget.service hidfwd.service kvmweb.service /etc/systemd/system/
sudo cp 99-kvm-usb-share.rules /etc/udev/rules.d/
sudo systemctl daemon-reload
sudo udevadm control --reload
sudo systemctl enable --now usb-kvm-gadget hidfwd kvmweb
```

**Cổng USB:**
- Cổng OTG (USB-A ngoài cùng bên trái, sát nút reset) = gadget → cắm cáp USB-A đực-đực tới PC đích.
- Card capture MS2109 **phải cắm cổng USB-A còn lại** (hub onboard), KHÔNG cắm cổng OTG — PHY cổng OTG được mux sang DWC2 cho gadget.

Mở `http://<ip-zte>:8090`. Độ phân giải đã chọn được lưu ở `kvmweb.json`.

Đường dẫn trong các unit mặc định là `/home/sang/Run/kvm`; sửa nếu cài chỗ khác.
