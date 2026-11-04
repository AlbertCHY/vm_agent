#!/bin/bash
# ==============================================================================
# 掃描初始系統碟容量 (含 block device rescan)，若有未分配空間則以 LVM 擴容根目錄 (/)
# 具備完整指令存在檢查、分割區前後容量比對與各階段錯誤訊息輸出
# ==============================================================================
set -uo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "[ERROR] Please use root or sudo to run this script."
    exit 1
fi

MIN_FREE_BYTES=$((1024 * 1024 * 1024)) # 至少有 1GB 剩餘未分配空間才執行擴容

echo "=== Root Disk Rescan & LVM Expansion Start ==="

# 0. 檢查必要基礎工具是否存在
for cmd in findmnt lsblk lvs pvs vgs pvresize lvextend; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "[ERROR] Required command '$cmd' is not installed on this VM."
        exit 2
    fi
done

ROOT_SOURCE=$(findmnt -n -o SOURCE / 2>&1)
if [[ -z "$ROOT_SOURCE" ]]; then
    echo "[ERROR] Failed to detect root mount source via findmnt."
    exit 3
fi

ROOT_TYPE=$(lsblk -no TYPE "$ROOT_SOURCE" 2>/dev/null | head -n 1 || true)
if [[ "$ROOT_TYPE" != "lvm" ]]; then
    echo "[INFO] Root source ($ROOT_SOURCE) is not on LVM (type=${ROOT_TYPE:-unknown}), skipping LVM root expansion."
    exit 0
fi

ROOT_VG=$(lvs --noheadings -o vg_name "$ROOT_SOURCE" 2>/dev/null | awk '{print $1}')
if [[ -z "$ROOT_VG" ]]; then
    echo "[ERROR] Failed to determine VG name for root LV '$ROOT_SOURCE'."
    exit 4
fi

# 找出該 VG 底下的 PV (若有連帶分割區的 PV，優先取最大或最後一個分割區)
ROOT_PV=$(pvs --noheadings -o pv_name -S "vg_name=$ROOT_VG" 2>/dev/null | awk '{print $1}' | tail -n 1)
if [[ -z "$ROOT_PV" || ! -b "$ROOT_PV" ]]; then
    echo "[ERROR] Cannot determine underlying PV block device for VG '$ROOT_VG' (got: '${ROOT_PV:-empty}')."
    exit 5
fi

PARENT_DISK_NAME=$(lsblk -no PKNAME "$ROOT_PV" 2>/dev/null | head -n 1 | tr -d '[:space:]')
PV_BASENAME=$(basename "$ROOT_PV")
PART_NUM=""
if [[ -f "/sys/class/block/${PV_BASENAME}/partition" ]]; then
    PART_NUM=$(cat "/sys/class/block/${PV_BASENAME}/partition" 2>/dev/null | tr -d '[:space:]')
elif [[ "$PV_BASENAME" =~ ([0-9]+)$ ]]; then
    PART_NUM="${BASH_REMATCH[1]}"
fi

if [[ -z "$PARENT_DISK_NAME" || -z "$PART_NUM" ]]; then
    echo "[ERROR] Cannot determine parent disk ('${PARENT_DISK_NAME}') or partition number ('${PART_NUM}') for PV '$ROOT_PV'."
    lsblk
    exit 6
fi

PARENT_DISK="/dev/$PARENT_DISK_NAME"
if [[ ! -b "$PARENT_DISK" ]]; then
    echo "[ERROR] Parent disk block device '$PARENT_DISK' does not exist."
    exit 6
fi

# ==============================================================================
# 1. 重新掃描區塊裝置容量 (讓核心感知 VMware 模板開機時放大的硬碟容量)
# ==============================================================================
RESCAN_PATH="/sys/class/block/${PARENT_DISK_NAME}/device/rescan"
if [[ -f "$RESCAN_PATH" ]]; then
    echo "[INFO] Triggering block device rescan: echo 1 > $RESCAN_PATH"
    if ! echo 1 > "$RESCAN_PATH" 2>/tmp/rescan_err.log; then
        echo "[WARN] Rescan write returned non-zero: $(cat /tmp/rescan_err.log 2>/dev/null)"
    fi
    rm -f /tmp/rescan_err.log
else
    echo "[INFO] $RESCAN_PATH not found, scanning all /sys/class/block/sd*/device/rescan..."
    for r in /sys/class/block/sd*/device/rescan; do
        [[ -f "$r" ]] && echo 1 > "$r" 2>/dev/null || true
    done
fi
udevadm settle &>/dev/null || sleep 1

# ==============================================================================
# 2. 檢查系統碟總容量與分割區現況
# ==============================================================================
DISK_SIZE=$(lsblk -bno SIZE -d "$PARENT_DISK" 2>/dev/null | awk '{print $1}')
PART_SUM=$(lsblk -bno SIZE,TYPE "$PARENT_DISK" 2>/dev/null | awk '$2=="part" {s+=$1} END {print s+0}')
BEFORE_PART_SIZE=$(lsblk -bno SIZE -d "$ROOT_PV" 2>/dev/null | awk '{print $1}')
BEFORE_LV_SIZE=$(lsblk -bno SIZE -d "$ROOT_SOURCE" 2>/dev/null | awk '{print $1}')
FREE_BYTES=$((DISK_SIZE - PART_SUM))

echo "[INFO] Root LV     : $ROOT_SOURCE (Current size: $((BEFORE_LV_SIZE / 1024 / 1024)) MB)"
echo "[INFO] Root PV     : $ROOT_PV on $PARENT_DISK partition #$PART_NUM (Current size: $((BEFORE_PART_SIZE / 1024 / 1024)) MB)"
echo "[INFO] Parent Disk : $PARENT_DISK (Total size: $((DISK_SIZE / 1024 / 1024)) MB, Unallocated: $((FREE_BYTES / 1024 / 1024)) MB)"

# ==============================================================================
# 3. 擴展分割區 (優先使用 growpart，若無則依 OS 安裝 cloud-utils，最後備援使用安全加固的 parted)
# ==============================================================================
if (( FREE_BYTES <= MIN_FREE_BYTES )); then
    echo "[INFO] No unallocated space (>1GB) on $PARENT_DISK (free=$((FREE_BYTES / 1024 / 1024)) MB), skipping partition resize."
else
    echo "[ACTION] Detected $((FREE_BYTES / 1024 / 1024)) MB (>1GB) unallocated space on $PARENT_DISK. Expanding partition $ROOT_PV..."
    EXPAND_LOG=""

    # 3.0 安全檢查：確認 ROOT_PV 是該磁碟實體位置最後一個分割區，避免覆蓋或重疊後方分割區
    LAST_PART=""
    MAX_START=-1
    for p in $(lsblk -nrpo NAME,TYPE "$PARENT_DISK" 2>/dev/null | awk '$2=="part"{print $1}'); do
        p_base=$(basename "$p")
        p_start=$(cat "/sys/class/block/${p_base}/start" 2>/dev/null || echo 0)
        if (( p_start > MAX_START )); then
            MAX_START=$p_start
            LAST_PART="$p"
        fi
    done

    if [[ -n "$LAST_PART" && "$LAST_PART" != "$ROOT_PV" ]]; then
        echo "[ERROR] Safety check failed: Root PV ($ROOT_PV) is NOT the last partition on $PARENT_DISK (last partition is $LAST_PART)."
        echo "[ERROR] Refusing to resize $ROOT_PV to avoid overlapping with subsequent partitions."
        lsblk "$PARENT_DISK"
        exit 9
    fi

    # 3.1 方法 A: 檢查並優先使用 growpart (若未安裝，先依 OS 版本嘗試安裝 cloud-utils)
    if ! command -v growpart &>/dev/null; then
        OS_FAMILY=""
        OS_LIKE=""
        if [[ -f /etc/os-release ]]; then
            # shellcheck disable=SC1091
            . /etc/os-release
            OS_FAMILY="${ID:-}"
            OS_LIKE="${ID_LIKE:-}"
        fi
        echo "[INFO] 'growpart' is not installed. Attempting to install cloud-utils for OS (ID=${OS_FAMILY:-unknown}, LIKE=${OS_LIKE:-unknown})..."

        if [[ "$OS_FAMILY" == "ubuntu" || "$OS_FAMILY" == "debian" || "$OS_LIKE" == *"debian"* || "$OS_LIKE" == *"ubuntu"* ]]; then
            if DEBIAN_FRONTEND=noninteractive apt-get update -y &>/tmp/cloud_utils_install.log && \
               DEBIAN_FRONTEND=noninteractive apt-get install -y cloud-guest-utils &>>/tmp/cloud_utils_install.log; then
                echo "[OK] Installed cloud-guest-utils via apt-get."
            else
                echo "[WARN] Failed to install cloud-guest-utils via apt-get."
                EXPAND_LOG+="[apt-install-growpart] $(tail -n 3 /tmp/cloud_utils_install.log 2>/dev/null | tr '\n' ' '); "
            fi
        elif [[ "$OS_FAMILY" == "centos" || "$OS_FAMILY" == "rhel" || "$OS_FAMILY" == "rocky" || "$OS_FAMILY" == "oracle" || "$OS_FAMILY" == "ol" || "$OS_FAMILY" == "almalinux" || "$OS_LIKE" == *"rhel"* || "$OS_LIKE" == *"fedora"* ]]; then
            PKG_MGR="yum"
            command -v dnf &>/dev/null && PKG_MGR="dnf"
            if "$PKG_MGR" install -y cloud-utils-growpart &>/tmp/cloud_utils_install.log; then
                echo "[OK] Installed cloud-utils-growpart via $PKG_MGR."
            else
                echo "[WARN] Failed to install cloud-utils-growpart via $PKG_MGR."
                EXPAND_LOG+="[${PKG_MGR}-install-growpart] $(tail -n 3 /tmp/cloud_utils_install.log 2>/dev/null | tr '\n' ' '); "
            fi
        else
            echo "[WARN] Unsupported OS family '${OS_FAMILY:-unknown}' for automatic cloud-utils installation."
            EXPAND_LOG+="[install-growpart] unsupported OS ${OS_FAMILY:-unknown}; "
        fi
        rm -f /tmp/cloud_utils_install.log
    fi

    if command -v growpart &>/dev/null; then
        echo "[INFO] Attempting partition expansion with 'growpart $PARENT_DISK $PART_NUM'..."
        if GROW_OUT=$(growpart "$PARENT_DISK" "$PART_NUM" 2>&1); then
            echo "[OK] growpart succeeded: $GROW_OUT"
        else
            echo "[WARN] growpart failed (exit=$?): $GROW_OUT"
            EXPAND_LOG+="[growpart] $GROW_OUT; "
        fi
        partprobe "$PARENT_DISK" 2>/dev/null || partx -u "$PARENT_DISK" 2>/dev/null || true
        udevadm settle &>/dev/null || sleep 1
    fi

    AFTER_PART_SIZE=$(lsblk -bno SIZE -d "$ROOT_PV" 2>/dev/null | awk '{print $1}')

    # 3.2 方法 B: 若 growpart 未能完成擴容，嘗試使用安全加固後的 parted
    if (( AFTER_PART_SIZE <= BEFORE_PART_SIZE )); then
        if command -v parted &>/dev/null; then
            echo "[INFO] Falling back to 'parted' for partition expansion..."

            # 先確保 GPT backup header 已移至磁碟尾端，避免 resizepart 同時跳出 Fix/Ignore 與 Yes/No 雙重詢問導致輸入錯位
            if command -v sgdisk &>/dev/null; then
                echo "[INFO] Running 'sgdisk -e $PARENT_DISK' to relocate GPT backup header..."
                SGDISK_OUT=$(sgdisk -e "$PARENT_DISK" 2>&1) || echo "[WARN] sgdisk -e output: $SGDISK_OUT"
            fi
            PARTED_CHECK=$(parted -s "$PARENT_DISK" print 2>&1 || true)
            if [[ "$PARTED_CHECK" == *"Fix/Ignore"* || "$PARTED_CHECK" == *"not at the end of the disk"* || "$PARTED_CHECK" == *"not all of the space"* ]]; then
                echo "[INFO] Fixing GPT backup table position via parted..."
                printf "Fix\n" | parted ---pretend-input-tty "$PARENT_DISK" print >/dev/null 2>&1 || true
            fi

            # 先嘗試標準非互動 -s 模式
            if PARTED_OUT=$(parted -s "$PARENT_DISK" resizepart "$PART_NUM" 100% 2>&1); then
                echo "[OK] parted -s resizepart succeeded."
            else
                echo "[WARN] 'parted -s resizepart' returned: $PARTED_OUT"
            fi

            partprobe "$PARENT_DISK" 2>/dev/null || partx -u "$PARENT_DISK" 2>/dev/null || true
            udevadm settle &>/dev/null || sleep 1
            AFTER_PART_SIZE=$(lsblk -bno SIZE -d "$ROOT_PV" 2>/dev/null | awk '{print $1}')

            # 若因分割區掛載中而需要互動確認 Yes/No，在已明確指定 100% 參數下僅單純送入 Yes
            if (( AFTER_PART_SIZE <= BEFORE_PART_SIZE )); then
                echo "[INFO] Retrying parted with ---pretend-input-tty (confirming in-use partition resize)..."
                if PARTED_TTY_OUT=$(printf "Yes\n" | parted ---pretend-input-tty "$PARENT_DISK" resizepart "$PART_NUM" 100% 2>&1); then
                    echo "[INFO] parted ---pretend-input-tty resizepart executed."
                else
                    EXPAND_LOG+="[parted] $PARTED_OUT | $PARTED_TTY_OUT; "
                fi
                partprobe "$PARENT_DISK" 2>/dev/null || partx -u "$PARENT_DISK" 2>/dev/null || true
                udevadm settle &>/dev/null || sleep 1
                AFTER_PART_SIZE=$(lsblk -bno SIZE -d "$ROOT_PV" 2>/dev/null | awk '{print $1}')
            fi
        else
            echo "[WARN] 'parted' is not installed on this VM."
            EXPAND_LOG+="[parted] command not found; "
        fi
    fi

    # 3.3 驗證分割區大小是否真的增加，若兩種方式皆失敗則直接報錯結束
    if (( AFTER_PART_SIZE <= BEFORE_PART_SIZE )); then
        echo "[ERROR] Failed to expand partition $ROOT_PV (both 'growpart' and 'parted' failed)."
        echo "[ERROR] Partition size remained $((BEFORE_PART_SIZE / 1024 / 1024)) MB -> $((AFTER_PART_SIZE / 1024 / 1024)) MB."
        echo "[ERROR] Expansion attempt logs: ${EXPAND_LOG:-none}"
        echo "[INFO] Current partition table of $PARENT_DISK:"
        fdisk -l "$PARENT_DISK" 2>&1 || lsblk "$PARENT_DISK"
        exit 10
    fi

    echo "[OK] Partition $ROOT_PV expanded: $((BEFORE_PART_SIZE / 1024 / 1024)) MB -> $((AFTER_PART_SIZE / 1024 / 1024)) MB"

    # ==============================================================================
    # 4. 執行 pvresize 並檢查錯誤
    # ==============================================================================
    echo "[ACTION] Running 'pvresize $ROOT_PV'..."
    if ! PV_OUT=$(pvresize "$ROOT_PV" 2>&1); then
        echo "[ERROR] 'pvresize $ROOT_PV' failed: $PV_OUT"
        exit 11
    fi
    echo "[OK] pvresize output: $(echo "$PV_OUT" | tr '\n' ' ')"
fi

# ==============================================================================
# 5. 檢查 VG 是否有可用 Free PE，若有則執行 lvextend -r 擴展 LV 與檔案系統
# ==============================================================================
VG_FREE_PE=$(vgs --noheadings -o vg_free_count "$ROOT_VG" 2>/dev/null | awk '{print $1}')
if [[ -z "$VG_FREE_PE" ]]; then
    echo "[ERROR] Failed to query free extents (vg_free_count) for VG '$ROOT_VG'."
    exit 12
fi

if (( VG_FREE_PE > 0 )); then
    echo "[ACTION] VG '$ROOT_VG' has $VG_FREE_PE free extents. Running 'lvextend -l +100%FREE -r $ROOT_SOURCE'..."
    if ! LV_OUT=$(lvextend -l +100%FREE -r "$ROOT_SOURCE" 2>&1); then
        echo "[ERROR] 'lvextend -l +100%FREE -r $ROOT_SOURCE' failed: $LV_OUT"
        exit 13
    fi
    echo "[OK] lvextend output: $(echo "$LV_OUT" | tr '\n' ' ')"

    AFTER_LV_SIZE=$(lsblk -bno SIZE -d "$ROOT_SOURCE" 2>/dev/null | awk '{print $1}')
    echo "[SUCCESS] Root LV ($ROOT_SOURCE) expanded: $((BEFORE_LV_SIZE / 1024 / 1024)) MB -> $((AFTER_LV_SIZE / 1024 / 1024)) MB"
else
    if (( FREE_BYTES > MIN_FREE_BYTES )); then
        echo "[ERROR] Partition expanded and pvresize ran, but VG '$ROOT_VG' still reports 0 free extents!"
        pvs
        vgs
        exit 14
    fi
    echo "[OK] No unallocated disk space or free VG extents on $PARENT_DISK. Root expansion not needed."
fi
df -h /
