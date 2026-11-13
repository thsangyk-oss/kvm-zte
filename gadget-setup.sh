#!/bin/bash
# Tao USB HID gadget (keyboard + mouse) tren cong OTG cua ZTE W105D/W103D.
# Chay voi quyen root:  sudo bash gadget-setup.sh
# Cong vat ly dung: cong USB-A ngoai cung ben trai (sat nut reset) -> cap USB-A duc-duc -> PC dich.
set -e

G=/sys/kernel/config/usb_gadget/kvm
UDC=ff400000.usb

# configfs can duoc mount
if [ ! -d /sys/kernel/config/usb_gadget ]; then
    mount -t configfs configfs /sys/kernel/config 2>/dev/null || true
fi
/usr/sbin/modprobe libcomposite       2>/dev/null || /sbin/modprobe libcomposite       2>/dev/null || true
/usr/sbin/modprobe usb_f_hid          2>/dev/null || /sbin/modprobe usb_f_hid          2>/dev/null || true
/usr/sbin/modprobe usb_f_mass_storage 2>/dev/null || /sbin/modprobe usb_f_mass_storage 2>/dev/null || true

if [ -d "$G" ]; then
    echo "Gadget da ton tai, dang xoa de tao lai..."
    echo "" > "$G/UDC" 2>/dev/null || true
    rm -f "$G"/configs/c.1/hid.* "$G"/configs/c.1/mass_storage.* 2>/dev/null || true
    find "$G"/configs -name "hid.*" -o -name "mass_storage.*" | xargs -r rm -f 2>/dev/null || true
    rmdir "$G"/configs/c.1/strings/0x409 "$G"/configs/c.1 2>/dev/null || true
    rmdir "$G"/functions/hid.usb0 "$G"/functions/hid.usb1 "$G"/functions/hid.usb2 "$G"/functions/mass_storage.usb0 2>/dev/null || true
    rmdir "$G"/strings/0x409 "$G" 2>/dev/null || true
fi

mkdir -p "$G"
cd "$G"

echo 0x1d6b > idVendor    # Linux Foundation
echo 0x0104 > idProduct   # Multifunction Composite Gadget
echo 0x0100 > bcdDevice
echo 0x0200 > bcdUSB

mkdir -p strings/0x409
echo "DevinKVM"        > strings/0x409/manufacturer
echo "KVM HID Gadget"  > strings/0x409/product
echo "000000000001"    > strings/0x409/serialnumber

mkdir -p configs/c.1/strings/0x409
echo 250   > configs/c.1/MaxPower
echo "HID" > configs/c.1/strings/0x409/configuration

# --- Function 1: keyboard (boot protocol, report 8 byte) ---
mkdir -p functions/hid.usb0
echo 1 > functions/hid.usb0/protocol   # keyboard
echo 1 > functions/hid.usb0/subclass   # boot interface
echo 8 > functions/hid.usb0/report_length
printf '\x05\x01\x09\x06\xa1\x01\x05\x07\x19\xe0\x29\xe7\x15\x00\x25\x01\x75\x01\x95\x08\x81\x02\x95\x01\x75\x08\x81\x03\x95\x05\x75\x01\x05\x08\x19\x01\x29\x05\x91\x02\x95\x01\x75\x03\x91\x03\x95\x06\x75\x08\x15\x00\x25\x65\x05\x07\x19\x00\x29\x65\x81\x00\xc0' \
    > functions/hid.usb0/report_desc

# --- Function 2: mouse (boot protocol, report 4 byte: btn,dx,dy,wheel) ---
mkdir -p functions/hid.usb1
echo 2 > functions/hid.usb1/protocol   # mouse
echo 1 > functions/hid.usb1/subclass
echo 4 > functions/hid.usb1/report_length
printf '\x05\x01\x09\x02\xa1\x01\x09\x01\xa1\x00\x05\x09\x19\x01\x29\x03\x15\x00\x25\x01\x95\x03\x75\x01\x81\x02\x95\x01\x75\x05\x81\x03\x05\x01\x09\x30\x09\x31\x09\x38\x15\x81\x25\x7f\x75\x08\x95\x03\x81\x06\xc0\xc0' \
    > functions/hid.usb1/report_desc

# --- Function 3: con tro tuyet doi dang Digitizer Pen (cho web KVM) ---
# Android KHONG nhan "absolute mouse" (ABS_X/Y + BTN_LEFT, khong co BTN_TOUCH)
# -> bo qua. Pen (Usage Page 0x0D / Usage 0x02): kernel dat INPUT_PROP_DIRECT +
# BTN_TOUCH + BTN_TOOL_PEN -> Android coi la man hinh cham (cham dung toa do);
# Windows (Windows Ink) / Linux desktop cung nhan, con tro di theo khi hover.
# report 5 byte: bits(1: b0=Tip, b1=Barrel, b2=InRange) X(2 LE, 0-32767) Y(2)
mkdir -p functions/hid.usb2
echo 0 > functions/hid.usb2/protocol   # none
echo 0 > functions/hid.usb2/subclass
echo 5 > functions/hid.usb2/report_length
printf '\x05\x0d\x09\x02\xa1\x01\x09\x20\xa1\x00\x09\x42\x09\x44\x09\x32\x15\x00\x25\x01\x75\x01\x95\x03\x81\x02\x95\x05\x81\x03\x05\x01\x09\x30\x09\x31\x15\x00\x26\xff\x7f\x35\x00\x46\xff\x7f\x75\x10\x95\x02\x81\x02\xc0\xc0' \
    > functions/hid.usb2/report_desc

# --- Function 4: mass storage (USB luu tru cam vao ZTE -> PC dich thay) ---
# lun.0 removable: co the gan /dev/sdX vao file bat cu luc nao (usb-share.sh),
# file rong = "rut the nho". KHONG duoc mount tren ZTE khi dang gan cho PC.
mkdir -p functions/mass_storage.usb0
echo 1 > functions/mass_storage.usb0/lun.0/removable
echo 1 > functions/mass_storage.usb0/lun.0/nofua
echo "ZTE KVM Storage" > functions/mass_storage.usb0/lun.0/inquiry_string

ln -sfn functions/hid.usb0 configs/c.1/
ln -sfn functions/hid.usb1 configs/c.1/
ln -sfn functions/hid.usb2 configs/c.1/
ln -sfn functions/mass_storage.usb0 configs/c.1/

echo "$UDC" > UDC

# Lat mux USB trong chip: route USB2-PHY1 (cong OTG) sang DWC2 (device).
# QUAN TRONG: KHONG dung /sys/class/usb_role/*/role -> role=device dat ca
# bit U2D_ACT lan P21_SLEEP_M0 (usb_glue R4.1), lam DWC3 chet -> xhci "HC died"
# -> mat toan bo USB host (card capture MS2109 chet theo).
# Chi set U2D_ACT (bit31 USB_R0 @ glue base+0x80): DWC3 van chay host cho cac
# cong USB-A con lai (usb2-phy0 = hub onboard), chi PHY1 chuyen sang DWC2.
# => Card capture PHAI cam vao cong USB-A thuong (qua hub), KHONG duoc cam
# vao cong OTG (cong ngoai cung ben trai sat nut reset dung cho cable toi PC).
# Thu tu an toan (giong driver GXL): tat port host cua PHY1 truoc
# (R1.U3H_HOST_U2_PORT_DISABLE bit13) roi moi bat U2D_ACT — neu khong, viec
# rut PHY khi port dang stream co the lam xhci "not responding -> assume dead".
# Ngoai ra phai clear u2p-1.R0.HOST_DEVICE (bit0 @ +0x20): dat PHY1 sang che do
# device, khong thi dwc2 khong tao duoc link (UDC cu "not attached").
# Layout ffe09000: u2p-0 @0x00, u2p-1 @0x20, usb_glue @0x80.
python3 - <<'EOF'
import mmap, os, time
fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
m = mmap.mmap(fd, 0x200, offset=0xffe09000)
r1 = int.from_bytes(m[0x84:0x88], "little")
m[0x84:0x88] = (r1 | 0x2000).to_bytes(4, "little")   # disable host U2 port1
time.sleep(0.6)
u1 = int.from_bytes(m[0x20:0x24], "little")
m[0x20:0x24] = (u1 & ~1).to_bytes(4, "little")       # u2p-1 HOST_DEVICE=0
r0 = int.from_bytes(m[0x80:0x84], "little")
m[0x80:0x84] = (r0 | 0x80000000).to_bytes(4, "little")  # U2D_ACT: phy1 -> dwc2
print("OTG -> device: R0=0x%08x R1=0x%08x u2p1=0x%08x" % (int.from_bytes(m[0x80:0x84], "little"), int.from_bytes(m[0x84:0x88], "little"), int.from_bytes(m[0x20:0x24], "little")))
EOF

# Cho user group input (chay forwarder khong can root)
sleep 0.5
for h in /dev/hidg*; do
    [ -e "$h" ] && chgrp input "$h" && chmod 660 "$h"
done

echo "OK - gadget da tao. Khi cam vao PC se thay ban phim + chuot USB."
ls -l /dev/hidg* 2>/dev/null || echo "(hidg se xuat hien khi cam vao host)"
