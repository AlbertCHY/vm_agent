"""公司「伺服器資源申請與歸還」表單的預處理器 (原 preprocessor.py)

以正規表示式從表單純文字中擷取結構化欄位，提供給 Ollama 工單解析作為提示並補值。
"""
import re
from typing import Any

def preprocess_ticket(raw_text: str) -> dict[str, Any]:
    """
    預處理申請工單 Raw Text。
    提取：單號、申請人、工號、申請單位、專案名稱、專案背景、有效審核意見、資源統計、VM主機表格。
    """
    if not raw_text or not isinstance(raw_text, str):
        return {
            "單號": "未提供",
            "申請人": "未提供",
            "申請人工號": "未提供",
            "申請單位": "未提供",
            "專案名稱": "未提供",
            "資源管理人": "未提供",
            "資源管理人工號": "未提供",
            "專案背景": "未提供",
            "審核覆寫意見": [],
            "資源總計": {"VM總數": 1, "vCore總數": 0, "Memory總數GB": 0, "HDD總數GB": 0, "SSD總數GB": 0},
            "VM表格": ""
        }

    # 標準化換行符號
    normalized_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")

    def extract_section(start_pattern: str, end_pattern: str, default: str = "") -> str:
        pattern = rf"(?:{start_pattern})\s*(.*?)(?=(?:{end_pattern})|$)"
        match = re.search(pattern, normalized_text, re.DOTALL | re.IGNORECASE)
        if match and match.group(1) is not None:
            return match.group(1).strip()
        return default

    def extract_field(pattern: str, section: str = "", default: str = "") -> str:
        # 先在指定區塊內搜尋
        if section:
            m = re.search(pattern, section, re.IGNORECASE)
            if m and m.group(1) is not None:
                return m.group(1).strip()
        return default

    cleaned_info: dict[str, Any] = {}

    # 1. 基本資訊與單號
    title_section = extract_section(r"伺服器資源申請與歸還", r"\n簽核節點記錄")
    cleaned_info["單號"] = extract_field(r"單號[：:\s]+([A-Za-z0-9_-]+)", title_section, "未提供")

    # 2. 申請人資訊
    applier_section = extract_section(r"使用者信息", r"\n申請內容")
    cleaned_info["申請人"] = extract_field(r"(?:姓名)[\s]+([^\n]+)", applier_section, "未提供")
    cleaned_info["申請人工號"] = extract_field(r"(?:工號)[\s]+([A-Za-z0-9_-]+)", applier_section, "未提供")
    cleaned_info["申請單位"] = extract_field(r"(?:事業處)[\s]+([^\n]+)", applier_section, "未提供")

    # 3. 專案資訊
    proj_section = extract_section(r"專案信息", r"\n需求資源")
    cleaned_info["專案名稱"] = extract_field(r"專案名稱\(英文\)[\s]+([^\n]+)", proj_section, "未提供")
    cleaned_info["資源管理人"] = extract_field(r"(?:資源管理員姓名)[\s]+([^\n]+)", proj_section, "未提供")
    cleaned_info["資源管理人工號"] = extract_field(r"(?:資源管理員工號)[\s]+([A-Za-z0-9_-]+)", proj_section, "未提供")
    cleaned_info["專案背景"] = extract_field(r"專案背景[\s]+([^\n]+)", proj_section, "無")

    # 4. 簽核節點記錄 (有效簽核意見，過濾掉自動跳過等贅詞)
    approval_section = extract_section(r"簽核節點記錄", r"\n使用者信息申請內容")
    approval_comments = []
    # 搜尋簽核意見或審核意見
    comment_pattern = r"(?:簽核意見[：:\s]+(.*?)(?=\n|$))"
    for match in re.finditer(comment_pattern, approval_section):
        comment = match.group(1).strip()
        # 過濾掉自動跳過、系統跳過或純空白
        if comment and "自動跳過" not in comment:
            approval_comments.append(comment)

    cleaned_info["審核覆寫意見"] = approval_comments

    # 5. 資源匯總數據
    res_section = extract_section(r"需求資源", r"\nVM主機規格")
    def safe_int(pattern: str, default: int = 0) -> int:
        val_str = extract_field(pattern, res_section, str(default))
        nums = re.findall(r"\d+", val_str)
        return int(nums[0]) if nums else default

    cleaned_info["資源總計"] = {
        "VM總數": safe_int(r"VM\s*總數[\s]+(\d+)", 1),
        "vCore總數": safe_int(r"vCore\s*總數[\s]+(\d+)", 0),
        "Memory總數GB": safe_int(r"總需求[\s]*Memory\(GB\)[\s]+(\d+)", 0),
        "HDD總數GB": safe_int(r"HDD[\s]*Storage\(GB\)[\s]+(\d+)", 0),
        "SSD總數GB": safe_int(r"SSD[\s]*Storage\(GB\)[\s]+(\d+)", 0)
    }

    # 6. VM 主機規格表格
    vm_table_section = extract_section(r"VM主機規格", r"\n前往")   
    cleaned_info["VM表格"] = vm_table_section.strip()

    return cleaned_info