#!/bin/bash
# Build + cai encoder H.264 phan cung (HCodec) cho S905X2/G12A tren kernel mainline (ophub 6.18).
# Nguon: github.com/ray38080289/meson-sm1-codecs (GPL-2.0, viet cho SM1) + g12a.patch (nhan G12A, bat nguon dung bit).
#   sudo tools/amvenc/build.sh          build + cai module amvenc_avc, firmware, xd-venc, tu nap khi boot
# Can: linux-headers-$(uname -r) (ophub co san /usr/src), gcc, make, git, python3, libturbojpeg0-dev.
# Phai chay lai sau moi lan doi kernel.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=${SRC:-/home/sang/Run/meson-sm1-codecs}
COMMIT=eb7a444
K=$(uname -r)
[ "$(id -u)" = 0 ] || { echo "chay bang sudo"; exit 1; }
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends gcc make git python3 libturbojpeg0-dev >/dev/null
[ -d "$SRC/.git" ] || sudo -u sang git clone https://github.com/ray38080289/meson-sm1-codecs "$SRC"
cd "$SRC"
git checkout -q "$COMMIT" && git checkout -q -- . && git apply "$HERE/g12a.patch"
make -C /lib/modules/$K/build M=$SRC/venc-h264 modules -j4
install -Dm644 venc-h264/amvenc_avc.ko /lib/modules/$K/updates/amvenc_avc.ko
depmod -a
# firmware: chi can ucode encoder (get-firmware.py kiem md5)
T=$(mktemp -d); python3 get-firmware.py --dest "$T" >/dev/null
install -Dm644 "$T/meson/venc/ga_h264_enc_cabac.bin" /lib/firmware/meson/venc/ga_h264_enc_cabac.bin; rm -rf "$T"
echo amvenc_avc > /etc/modules-load.d/amvenc.conf
gcc -O2 -Wall -o /usr/local/bin/xd-venc "$HERE/../xd-venc/xd-venc.c" -lturbojpeg -lpthread
sync
modprobe amvenc_avc && ls -l /dev/amvenc_avc
echo "XONG: amvenc_avc ($K) + firmware + /usr/local/bin/xd-venc; restart kvmweb de dung H.264"
