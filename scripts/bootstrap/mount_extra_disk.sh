#!/bin/bash
# ==============================================================================
# 掃描額外硬碟 (如 /dev/sdb)，若有則建立 LVM (datavg01/datalv01) 並掛載至 /data
# 具備完整指令偵測與各階段錯誤訊息輸出
# ==============================================================================
set -uo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "[ERROR] Please use root or sudo to run this script."
    exit 1
fi

VG_NAME="${VG_NAME:-datavg01}"
LV_NAME="${LV_NAME:-datalv01}"
MOUNT_POINT="${MOUNT_POINT:-/data}"

echo "=== Extra Data Disk LVM Mount Start ==="

for cmd in findmnt lsblk parted pvcreate vgcreate lvcreate mkfs.ext4 blkid; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "[ERROR] Required command '$cmd' is not installed on this VM."
        exit 2
    fi
done

if findmnt -n "$MOUNT_POINT" &>/dev/null; then
    echo "[OK] $MOUNT_POINT is already mounted ($(findmnt -n -o SOURCE,SIZE,USED,AVAIL "$MOUNT_POINT")). Skipping extra disk setup."
    df -h "$MOUNT_POINT"
    exit 0
fi

# 先觸發 SCSI Host 掃描，確保新掛上的額外硬碟已被核心識別
for host_scan in /sys/class/scsi_host/host*/scan; do
    [[ -f "$host_scan" ]] && echo "- - -" > "$host_scan" 2>/dev/null || true
done
udevadm settle &>/dev/null || sleep 1

# 若外部未指定 DISK_NAME，自動尋找沒有任何分割區、未掛載、非 PV 的空白磁碟 (如 sdb)
DISK_NAME="${DISK_NAME:-}"
if [[ -z "$DISK_NAME" ]]; then
    for d in $(lsblk -ndo NAME,TYPE | awk '$2=="disk"{print $1}'); do
        CHILD_COUNT=$(lsblk -no NAME "/dev/$d" | wc -l)
        MOUNTED=$(lsblk -no MOUNTPOINT "/dev/$d" | grep -v '^$' || true)
        IS_PV=$(pvs --noheadings -o pv_name 2>/dev/null | grep -F "/dev/$d" || true)
        if [[ "$CHILD_COUNT" -eq 1 && -z "$MOUNTED" && -z "$IS_PV" ]]; then
            DISK_NAME="$d"
            break
        fi
    done
fi

if [[ -z "$DISK_NAME" ]]; then
    echo "[OK] No additional unconfigured disk found. Nothing to mount to $MOUNT_POINT."
    exit 0
fi

DEVICE="/dev/$DISK_NAME"
if [[ "$DEVICE" =~ [0-9]$ ]]; then
    PARTITION="${DEVICE}p1"
else
    PARTITION="${DEVICE}1"
fi

if [[ ! -b "$DEVICE" ]]; then
    echo "[ERROR] Cannot find block device $DEVICE"
    exit 3
fi

echo "[ACTION] Found additional disk $DEVICE. Creating GPT partition & LVM ($VG_NAME/$LV_NAME)..."
if ! PART_OUT=$(parted -s "$DEVICE" mklabel gpt mkpart primary ext4 0% 100% 2>&1); then
    echo "[ERROR] 'parted' failed on $DEVICE: $PART_OUT"
    exit 4
fi
partprobe "$DEVICE" 2>/dev/null || partx -u "$DEVICE" 2>/dev/null || true
udevadm settle &>/dev/null || sleep 1

if [[ ! -b "$PARTITION" ]]; then
    echo "[ERROR] Partition block device '$PARTITION' was not created after running parted."
    lsblk "$DEVICE"
    exit 5
fi

if ! PV_OUT=$(pvcreate -f -y "$PARTITION" 2>&1); then
    echo "[ERROR] 'pvcreate $PARTITION' failed: $PV_OUT"
    exit 6
fi
echo "[OK] pvcreate: $(echo "$PV_OUT" | tr '\n' ' ')"

if ! VG_OUT=$(vgcreate "$VG_NAME" "$PARTITION" 2>&1); then
    echo "[ERROR] 'vgcreate $VG_NAME $PARTITION' failed: $VG_OUT"
    exit 7
fi
echo "[OK] vgcreate: $(echo "$VG_OUT" | tr '\n' ' ')"

if ! LV_OUT=$(lvcreate -y -l +100%FREE -n "$LV_NAME" "$VG_NAME" 2>&1); then
    echo "[ERROR] 'lvcreate -n $LV_NAME $VG_NAME' failed: $LV_OUT"
    exit 8
fi
echo "[OK] lvcreate: $(echo "$LV_OUT" | tr '\n' ' ')"

LV_PATH="/dev/$VG_NAME/$LV_NAME"
echo "--- Formatting $LV_PATH as ext4 ---"
if ! MKFS_OUT=$(mkfs.ext4 -F "$LV_PATH" 2>&1); then
    echo "[ERROR] 'mkfs.ext4 $LV_PATH' failed: $MKFS_OUT"
    exit 9
fi

echo "--- Mounting $LV_PATH to $MOUNT_POINT ---"
mkdir -p "$MOUNT_POINT"
if ! MNT_OUT=$(mount "$LV_PATH" "$MOUNT_POINT" 2>&1); then
    echo "[ERROR] 'mount $LV_PATH $MOUNT_POINT' failed: $MNT_OUT"
    exit 10
fi

UUID=$(blkid -s UUID -o value "$LV_PATH")
if [[ -n "$UUID" ]]; then
    echo "Found UUID: $UUID"
    if grep -q "$UUID" /etc/fstab; then
        echo "[WARN] UUID $UUID already exists in /etc/fstab, skipping write."
    else
        echo "UUID=$UUID  $MOUNT_POINT  ext4  defaults,nofail  0  2" >> /etc/fstab
        echo "[SUCCESS] Wrote UUID=$UUID ($MOUNT_POINT) to /etc/fstab."
    fi
else
    echo "[ERROR] Failed to retrieve UUID for $LV_PATH."
    exit 11
fi

echo "=== Extra Disk LVM Mount Completed ==="
df -h "$MOUNT_POINT"
