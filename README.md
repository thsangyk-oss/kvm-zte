# kvm-zte

Web KVM over IP chạy trên box ZTE W103D/W105D (Armbian):

- **Hình**: lấy màn hình máy đích qua HDMI capture USB (MacroSilicon MS2109, MJPEG), stream qua WebSocket với flow-control theo ack → độ trễ thấp, fps tự thích nghi theo mạng.
- **Chuột/phím**: ZTE giả lập bàn phím + chuột (tương đối và tuyệt đối) qua USB gadget (configfs HID) trên cổng OTG nối với máy đích.
- **Giao diện web** (`:8090`): máy tính và điện thoại (touchpad/chạm trực tiếp, pinch zoom, bàn phím ảo + hàng phím phụ, chọn độ phân giải/fps).

## Thành phần

| File | Vai trò |
|---|---|
| `gadget-setup.sh` | Tạo USB HID gadget (`/dev/hidg0` phím, `hidg1` chuột tương đối, `hidg2` chuột tuyệt đối), chuyển cổng OTG sang device |
| `hidfwd.py` | Ghi report HID; nhận lệnh UDP `127.0.0.1:5115`; chuyển tiếp chuột/phím cắm trực tiếp vào ZTE |
| `kvmweb.py` | HTTP + WebSocket server: capture V4L2 (không cần ffmpeg), `/ws`, `/stream`, `/snap`, `/health` |
| `kvm.html` | Giao diện web |
| `sendinput.py` | Client Linux: dùng chuột/phím của máy điều khiển qua UDP (phím ScrollLock để chuyển) |
| `*.service` | Unit systemd |

## Cài đặt

```bash
sudo cp usb-kvm-gadget.service hidfwd.service kvmweb.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now usb-kvm-gadget hidfwd kvmweb
```

Mở `http://<ip-zte>:8090`. Độ phân giải đã chọn được lưu ở `kvmweb.json`.

Đường dẫn trong các unit mặc định là `/home/sang/Run/kvm`; sửa nếu cài chỗ khác.
