import base64
import json
import shlex
import subprocess
import sys

import pytest

from clients.ssh_client import SSHClient
from subgraphs.common.review import ReviewDecision, parse_edits
from tools.vm_diag_tools import is_forbidden_path, mask_secrets


# ─── 審核輸入解析 ───
def test_parse_edits_keeps_raw_strings():
    assert parse_edits("cpu_count=8, project = proj-demo ,bad, x=") == {
        "cpu_count": "8", "project": "proj-demo", "x": "",
    }
    assert parse_edits("") == {}


def test_review_decision_validation():
    d = ReviewDecision.model_validate({"action": "edit", "edits": {"cpu_count": "8"}})
    assert d.feedback == "" and d.edits == {"cpu_count": "8"}


# ─── 機密環境變數傳遞 ───
def test_secret_env_never_in_command_line():
    cmd = SSHClient.build_command(
        "/tmp/set_passwords.sh", env={"USER1": "user1"}, sudo=True, env_file="/tmp/.aria_env_abc",
    )
    assert "Secr3t" not in cmd
    assert cmd.startswith("sudo -n env USER1=user1 bash -c ")
    inner = shlex.split(cmd)[-1]
    # 先載入、立即刪除，再執行腳本
    assert inner.index(". /tmp/.aria_env_abc") < inner.index("rm -f /tmp/.aria_env_abc") < inner.index("/tmp/set_passwords.sh")


def test_build_command_variants():
    assert SSHClient.build_command("whoami") == "whoami"
    assert SSHClient.build_command("x.sh", env={"A": "1 2"}) == "A='1 2' x.sh"
    assert SSHClient.build_command("x.sh", sudo=True, sudo_password=True).startswith("sudo -S -p '' env bash -c")


def test_env_file_quoting():
    content = SSHClient.build_env_file({"PW_ROOT": "a'b c$1", "EMPTY": None})
    assert content == "PW_ROOT='a'\"'\"'b c$1'\n"


def test_windows_command_and_secret_isolation():
    assert SSHClient.build_windows_command("whoami") == "whoami"
    assert SSHClient._to_sftp_path(r"C:\Temp\vm_bootstrap", os_type="windows") == "/C:/Temp/vm_bootstrap"
    assert SSHClient._to_win_path("/C:/Temp/vm_bootstrap/set_passwords.ps1") == r"C:\Temp\vm_bootstrap\set_passwords.ps1"

    secret_json = SSHClient.build_windows_secret_file({"PW_USER1": "Secr3t'\"$!", "EMPTY": None})
    assert json.loads(secret_json) == {"PW_USER1": "Secr3t'\"$!"}

    cmd = SSHClient.build_windows_command(
        "C:/Temp/vm_bootstrap/set_passwords.ps1",
        env={"USER1": "user1", "ZABBIX_CONF": r"C:\Program Files\Zabbix Agent\zabbix_agentd.conf"},
        env_file="/C:/Temp/vm_bootstrap/.aria_env_abc.json",
    )
    assert cmd.startswith("powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand ")
    b64 = cmd.rsplit(" ", 1)[-1]
    decoded = base64.b64decode(b64).decode("utf-16-le")
    assert "Secr3t" not in cmd and "Secr3t" not in decoded
    # 先讀取暫存 JSON、立刻刪除，再執行目標腳本
    assert decoded.index("ConvertFrom-Json") < decoded.index("Remove-Item") < decoded.index(r"& 'C:\Temp\vm_bootstrap\set_passwords.ps1'")


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows PowerShell")
def test_windows_command_live_powershell_execution_and_secret_cleanup(tmp_path):
    """實測經由 cmd.exe 執行 build_windows_command：驗證環境變數注入、機密檔即讀即刪、與非零 exit code 回傳"""
    script = tmp_path / "sample.ps1"
    secret_file = tmp_path / ".aria_env_test.json"
    pw = "S3cr!t'\"$%#1"
    secret_file.write_text(SSHClient.build_windows_secret_file({"PW_TEST": pw}), encoding="utf-8")
    script.write_text(
        f"if (Test-Path -LiteralPath '{secret_file}') {{ Write-Output 'LEAKED'; exit 99 }}\n"
        'Write-Output ("PUB=" + $env:PUB + "|SEC=" + $env:PW_TEST)\n'
        "exit 7\n",
        encoding="utf-8-sig",
    )
    cmd = SSHClient.build_windows_command(str(script), env={"PUB": "hello world"}, env_file=str(secret_file))
    res = subprocess.run(["cmd.exe", "/c", cmd], capture_output=True, text=True, timeout=30)
    assert res.returncode == 7
    assert res.stdout.strip() == f"PUB=hello world|SEC={pw}"
    assert not secret_file.exists()


# ─── 診斷輸出遮罩與檔案攔截 ───
def test_mask_secrets():
    text = (
        "DB_PASSWORD=\"hunter2\"\n"
        "api_key: abcdef\n"
        "proxy=http://user:pa55@proxy:8080\n"
        "PasswordAuthentication yes\n"
        "password    requisite    pam_pwquality.so retry=3\n"
    )
    masked = mask_secrets(text)
    assert "hunter2" not in masked and "abcdef" not in masked and "pa55" not in masked
    assert "PasswordAuthentication yes" in masked
    assert "pam_pwquality.so retry=3" in masked


def test_forbidden_paths():
    assert is_forbidden_path("/etc/shadow")
    assert is_forbidden_path("/etc/../etc/gshadow-")
    assert is_forbidden_path("/home/u/.ssh/id_ed25519")
    assert is_forbidden_path("/opt/app/server.KEY")
    assert not is_forbidden_path("/etc/ssh/sshd_config")
    assert not is_forbidden_path("/home/u/.ssh/authorized_keys")


def test_run_diag_script_falls_back_when_sudo_fails(monkeypatch):
    import tools.vm_diag_tools as diag

    calls = []

    class FakeSSH:
        def exec_command(self, ip, cmd, sudo=False, env=None, cmd_timeout=None):
            calls.append(sudo)
            if sudo:
                return 1, "", "sudo: a password is required"
            return 0, "=== [SECTION:BASIC] ===\nok-without-sudo", ""

    monkeypatch.setattr(diag, "get_ssh_client", lambda: FakeSSH())
    code, out, err, note = diag._run_diag_script("10.0.0.10", "diag_system_health.sh")
    assert calls == [True, False]
    assert code == 0 and "ok-without-sudo" in out
    assert "自動降級" in note


def test_extract_sections_and_inspect_vm_detail_validation():
    import tools.vm_diag_tools as diag

    sec = diag._extract_sections("header\n=== [SECTION:ONE] ===\nline1\n=== [SECTION:TWO] ===\nline2\n")
    assert sec["ONE"] == "line1" and sec["TWO"] == "line2"

    # 不支援的 check_type
    assert "不支援的 `check_type`" in diag.inspect_vm_detail.invoke({"ip": "10.0.0.10", "check_type": "exec", "target": "id"})
    # 非法服務名 (防止指令注入字元)
    assert "無效的服務名稱格式" in diag.inspect_vm_detail.invoke({"ip": "10.0.0.10", "check_type": "service", "target": "sshd;id"})
    # 禁止讀取敏感檔案
    assert "禁止讀取" in diag.inspect_vm_detail.invoke({"ip": "10.0.0.10", "check_type": "file", "target": "/etc/shadow"})
    # 非法程序/Port 關鍵字
    assert "無效的程序或 Port 關鍵字" in diag.inspect_vm_detail.invoke({"ip": "10.0.0.10", "check_type": "process_or_port", "target": "80 && id"})


def test_ssh_ensure_initial_login_pam_and_fallback(monkeypatch, tmp_path):
    """驗證 ensure_initial_login 的 PAM 過期強制改密碼與 SSH 私鑰降級路徑"""
    ssh = SSHClient(key_path=tmp_path / "missing_key")

    # 1. 初始密碼過期 -> 觸發 PAM 強制改密碼 -> 新密碼驗證成功
    def fake_exec_pam(ip, cmd, username=None, password=None, cmd_timeout=None):
        if password == "old-pw":
            return 1, "WARNING: Your password has expired", ""
        if password == "new-pw":
            return 0, "user1\n", ""
        return ssh.ERR_AUTH, "", "auth failed"

    monkeypatch.setattr(ssh, "exec_command", fake_exec_pam)
    monkeypatch.setattr(ssh, "_handle_pam_expired_password", lambda *a: (True, "PAM 已改密碼"))
    ec, active_pw, pam_changed, msg = ssh.ensure_initial_login("10.0.0.10", "user1", "old-pw", "new-pw")
    assert ec == 0 and active_pw == "new-pw" and pam_changed is True and "PAM" in msg

    # 2. 無初始密碼時退回 SSH 私鑰連線
    fake_key = tmp_path / "id_rsa"
    fake_key.write_text("dummy", encoding="utf-8")
    ssh2 = SSHClient(key_path=fake_key)
    monkeypatch.setattr(
        ssh2,
        "exec_command",
        lambda ip, cmd, username=None, password=None, cmd_timeout=None: (0, "user1\n", "") if password is None else (ssh2.ERR_AUTH, "", "no"),
    )
    ec2, active_pw2, pam2, _ = ssh2.ensure_initial_login("10.0.0.10", "user1", None, "new-pw")
    assert ec2 == 0 and active_pw2 is None and pam2 is False
