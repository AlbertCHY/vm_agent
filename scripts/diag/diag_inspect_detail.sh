#!/usr/bin/env bash
# 唯讀診斷腳本：針對特定 systemd 服務、journal 日誌、設定檔/Log 檔或 Port/程序進行細部複查
# 必填環境變數：CHECK_TYPE (service | journal | file | process_or_port), TARGET, MAX_LINES
# 選填環境變數：FORBIDDEN_REGEX (ERE，不分大小寫) — file 模式下以 realpath 解析 symlink 後比對，命中即拒絕讀取
export LANG=C LC_ALL=C
CHECK_TYPE="${CHECK_TYPE:-}"
TARGET="${TARGET:-}"
MAX_LINES="${MAX_LINES:-80}"
FORBIDDEN_REGEX="${FORBIDDEN_REGEX:-}"

is_forbidden() {
    [ -n "$FORBIDDEN_REGEX" ] && printf '%s\n' "$1" | grep -qiE -- "$FORBIDDEN_REGEX"
}

case "$CHECK_TYPE" in
    service)
        systemctl status "$TARGET" --no-pager -n 30 2>&1
        echo ""
        echo "--- systemctl is-enabled / is-active ---"
        echo "enabled: $(systemctl is-enabled "$TARGET" 2>&1) | active: $(systemctl is-active "$TARGET" 2>&1)"
        ;;
    journal)
        journalctl -u "$TARGET" -n "$MAX_LINES" --no-pager 2>&1
        ;;
    file)
        REAL_TARGET="$(readlink -f -- "$TARGET" 2>/dev/null || echo "$TARGET")"
        if is_forbidden "$TARGET" || is_forbidden "$REAL_TARGET"; then
            echo "🚫 基於安全底線，禁止讀取密碼雜湊或私鑰檔案內容 ($TARGET -> $REAL_TARGET)"
            exit 0
        fi
        if [ ! -e "$TARGET" ]; then
            echo "❌ 路徑不存在: $TARGET"
        elif [ -d "$TARGET" ]; then
            echo "📁 目錄列表與權限 ($TARGET):"
            ls -laZ "$TARGET" 2>&1 | head -n "$MAX_LINES"
        else
            [ "$REAL_TARGET" != "$TARGET" ] && echo "🔗 符號連結實際指向: $REAL_TARGET"
            echo "📄 檔案屬性 ($TARGET):"
            ls -lZ "$TARGET" 2>&1
            lsattr "$TARGET" 2>/dev/null
            echo "--- 檔案內容 (最後 $MAX_LINES 行) ---"
            tail -n "$MAX_LINES" "$REAL_TARGET" 2>&1
        fi
        ;;
    process_or_port)
        echo "--- 程序搜尋 (ps aux | grep -i $TARGET) ---"
        ps aux | grep -i -- "$TARGET" | grep -v grep | head -n 30
        echo "--- Port / Socket 搜尋 (ss -tulpn | grep -i $TARGET) ---"
        ss -tulpn 2>&1 | grep -iE -- "State|$TARGET" | head -n 30
        ;;
    *)
        echo "❌ 未知的 CHECK_TYPE: $CHECK_TYPE"
        exit 1
        ;;
esac
