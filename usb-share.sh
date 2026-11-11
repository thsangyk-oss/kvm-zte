#!/bin/bash
# Gan/rut USB luu tru vao LUN mass_storage cua gadget -> PC dich thay nhu USB that.
# udev goi: usb-share.sh add|remove <kernel-name>   (vd: sda)
# Chay tay:  usb-share.sh rescan  (gan lai disk usb con lai sau khi doi cau hinh)
set -u

LUN=/sys/kernel/config/usb_gadget/kvm/functions/mass_storage.usb0/lun.0
STATE=/run/kvm-usb-share
LOG=/run/kvm-usb-share.log

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

exec 9>/run/kvm-usb-share.lock
flock -w 20 9 || { log "lock timeout"; exit 1; }

# Luc boot, udev trigger co the chay truoc gadget-setup: doi LUN toi da 15s.
for _ in $(seq 1 30); do [ -d "$LUN" ] && break; sleep 0.5; done
[ -d "$LUN" ] || { log "khong co LUN - gadget chua san sang"; exit 1; }

disk_of() {   # partition -> disk cha; in ra ten disk (khong co /dev)
    local d="${1#/dev/}"
    [ -b "/dev/$d" ] || { echo ""; return; }
    if [ "$(lsblk -ndo TYPE "/dev/$d" 2>/dev/null)" = "part" ]; then
        lsblk -ndo PKNAME "/dev/$d" 2>/dev/null | head -1
    else
        echo "$d"
    fi
}

umount_disk() {   # go moi mount cua disk tren ZTE truoc khi gan cho PC
    lsblk -ln -o MOUNTPOINT "/dev/$1" 2>/dev/null | while read -r mp; do
        [ -n "$mp" ] && umount "$mp" 2>/dev/null && log "umount $mp"
    done
}

assign() {
    [ -b "/dev/$1" ] || return 1
    umount_disk "$1"
    if echo "/dev/$1" > "$LUN/file" 2>/dev/null; then
        echo "$1" > "$STATE"
        log "gan /dev/$1 -> PC"
        return 0
    fi
    log "gan /dev/$1 that bai"
    return 1
}

rescan() {   # gan disk usb dau tien con lai
    local n tran
    while read -r n tran; do
        if [ "$tran" = "usb" ] && assign "$n"; then return 0; fi
    done < <(lsblk -dn -o NAME,TRAN 2>/dev/null)
    return 1
}

case "${1:-}" in
    add)
        d=$(disk_of "${2:-}")
        [ -n "$d" ] && assign "$d"
        ;;
    remove)
        d=$(disk_of "${2:-}")
        if [ "$d" = "$(cat "$STATE" 2>/dev/null)" ]; then
            echo > "$LUN/file" 2>/dev/null || true   # file rong = eject
            rm -f "$STATE"
            log "eject $d"
            rescan || true
        fi
        ;;
    rescan)
        rescan || log "khong tim thay disk usb nao"
        ;;
    detach)   # eject mem, KHONG gan lai disk khac (de flash image vao stick)
        echo "" > "$LUN/file" 2>/dev/null || true
        rm -f "$STATE"
        log "detach (eject mem)"
        ;;
    *)
        echo "usage: $0 add|remove <dev> | rescan | detach" >&2
        exit 2
        ;;
esac
