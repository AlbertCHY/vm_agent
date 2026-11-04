import secrets
import string
import random

try:
    import pyperclip
except ImportError:
    pyperclip = None

# 排除在 Bash / PowerShell 雙引號或指令列中會觸發變數展開或語法解析錯誤的符號 ($ ! & ( ) [ ] { } ? * ` \ ' ")
SAFE_SYMBOLS = "@#%^_+-="


def generate_strict_password(length: int = 12) -> str:
    """
    產生指定長度的強密碼，且強制包含：
    1. 至少一個大寫字母
    2. 至少一個小寫字母
    3. 至少一個數字
    4. 至少一個跨平台 Shell/PowerShell 安全的特殊符號 (@#%^_+-=)
    """
    if length < 4:
        raise ValueError("密碼長度至少需要 4 碼才能包含所有類型的字元。")
    if length > 30:
        raise ValueError("超過密碼長度產生上限。")

    # 1. 定義字元集
    upper_chars = string.ascii_uppercase
    lower_chars = string.ascii_lowercase
    digits = string.digits
    safe_symbols = SAFE_SYMBOLS

    # 全部合起來的池子
    all_chars = upper_chars + lower_chars + digits + safe_symbols

    # 2. 【強制階段】從每一類中各選出一個字元
    char_1 = secrets.choice(upper_chars)
    char_2 = secrets.choice(lower_chars)
    char_3 = secrets.choice(digits)
    char_4 = secrets.choice(safe_symbols)

    password_list = [char_1, char_2, char_3, char_4]

    # 3. 【填充階段】剩下的長度隨機選取
    for _ in range(length - 4):
        password_list.append(secrets.choice(all_chars))

    # 4. 【洗牌階段】使用 SystemRandom 打亂順序
    random.SystemRandom().shuffle(password_list)

    # 5. 轉回字串
    return "".join(password_list)


def generate_vm_passwords(length: int = 12) -> dict[str, str]:
    """
    產生單台 VM 初始化所需的兩組密碼：
    - admin_password: 供 root 與 user1 共用
    - user2_password: 供 user2 使用
    """
    return {
        "admin_password": generate_strict_password(length),
        "user2_password": generate_strict_password(length),
    }


if __name__ == "__main__":
    while True:
        try:
            user_input = input("請輸入密碼長度 (預設 12): ").strip() or "12"

            if not user_input.isdigit():
                raise ValueError("請輸入正整數")

            pwd = generate_strict_password(int(user_input))

            print("-" * 30)
            print(pwd)
            print("-" * 30)
            if pyperclip is not None:
                pyperclip.copy(pwd)
                input("密碼已複製到剪貼簿，按 Enter 鍵結束...")
            else:
                input("按 Enter 鍵結束...")
            break
        except ValueError as e:
            print(f"錯誤: {e}")
            continue
