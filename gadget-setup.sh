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
/usr/sbin/modprobe libcomposite 2>/dev/null || /sbin/modprobe libcomposite 2>/dev/null || true
/usr/sbin/modprobe usb_f_hid    2>/dev/null || /sbin/modprobe usb_f_hid    2>/dev/null || true

if [ -d "$G" ]; then
    echo "Gadget da ton tai, dang xoa de tao lai..."
    echo "" > "$G/UDC" 2>/dev/null || true
    rm -f "$G"/configs/c.1/hid.* 2>/dev/null || true
    find "$G"/configs -name "hid.*" -delete 2>/dev/null || true
    rmdir "$G"/configs/c.1/strings/0x409 "$G"/configs/c.1 2>/dev/null || true
    rmdir "$G"/functions/hid.usb0 "$G"/functions/hid.usb1 "$G"/functions/hid.usb2 2>/dev/null || true
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

# --- Function 3: absolute pointer (chuot tuyet doi cho web KVM) ---
# report 6 byte: buttons(1) X(2 LE, 0-32767) Y(2) wheel(1)
mkdir -p functions/hid.usb2
echo 0 > functions/hid.usb2/protocol   # none
echo 0 > functions/hid.usb2/subclass
echo 6 > functions/hid.usb2/report_length
printf '\x05\x01\x09\x02\xa1\x01\x09\x01\xa1\x00\x05\x09\x19\x01\x29\x03\x15\x00\x25\x01\x95\x03\x75\x01\x81\x02\x95\x01\x75\x05\x81\x03\x05\x01\x09\x30\x09\x31\x16\x00\x00\x26\xff\x7f\x75\x10\x95\x02\x81\x02\x09\x38\x15\x81\x25\x7f\x75\x08\x95\x01\x81\x06\xc0\xc0' \
    > functions/hid.usb2/report_desc

ln -sfn functions/hid.usb0 configs/c.1/
ln -sfn functions/hid.usb1 configs/c.1/
ln -sfn functions/hid.usb2 configs/c.1/

echo "$UDC" > UDC

# Lat mux USB trong chip: route USB2-PHY1 sang DWC2 (device).
# Mac dinh la "host" -> cong OTG bi khoa o che do host.
ROLE=/sys/class/usb_role/ffe09000.usb-role-switch/role
if [ -e "$ROLE" ]; then
    echo device > "$ROLE" && echo "Role switch -> device (cong OTG hoat dong gadget)"
else
    echo "CANH BAO: khong thay $ROLE"
fi

# Cho user group input (chay forwarder khong can root)
sleep 0.5
for h in /dev/hidg*; do
    [ -e "$h" ] && chgrp input "$h" && chmod 660 "$h"
done

echo "OK - gadget da tao. Khi cam vao PC se thay ban phim + chuot USB."
ls -l /dev/hidg* 2>/dev/null || echo "(hidg se xuat hien khi cam vao host)"
