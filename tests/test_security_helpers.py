import shlex

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
