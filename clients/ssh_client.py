"""Paramiko SSH / SFTP 封裝 (支援 Linux 與 Windows OpenSSH、私鑰與初始密碼登入、PAM 強制改密碼、SFTP 檔案/資料夾上傳、機密暫存檔注入)"""
import base64
import io
import ipaddress
import json
import posixpath
import re
import shlex
import socket
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Optional

import config

try:
    import paramiko
except ImportError:  # pragma: no cover - 於執行期提示安裝
    paramiko = None


class SSHClient:
    """
    無狀態 SSH / SFTP 執行器：每次呼叫皆建立新連線、執行完畢即關閉。
    所有連線/驗證/逾時錯誤皆被捕捉並轉為 (exit_code<0, "", 錯誤訊息)，不會向上拋出。
    """

    # 自訂錯誤碼 (遠端指令本身的 exit code 一律 >= 0)
    ERR_CONFIG = -1       # 參數或環境設定錯誤 (金鑰不存在、套件未安裝、IP 格式錯)
    ERR_AUTH = -2         # 金鑰或密碼無效 / 驗證失敗
    ERR_UNREACHABLE = -3  # 主機不可達 / 連線被拒
    ERR_TIMEOUT = -4      # 連線或指令逾時
    ERR_SSH = -5          # 其他 SSH / SFTP 協定錯誤

    def __init__(
        self,
        key_path: Optional[Path | str] = None,
        username: Optional[str] = None,
        port: Optional[int] = None,
        timeout: Optional[int] = None,
        passphrase: Optional[str] = None,
        strict_host_key: Optional[bool] = None,
    ):
        self.key_path = Path(key_path) if key_path else config.SSH_PRIVATE_KEY_PATH
        self.username = username or config.DEFAULT_SSH_USER
        self.port = port or config.SSH_PORT
        self.timeout = timeout or config.SSH_TIMEOUT
        self.passphrase = passphrase if passphrase is not None else config.SSH_KEY_PASSPHRASE
        self.strict_host_key = config.SSH_STRICT_HOST_KEY if strict_host_key is None else strict_host_key

    def _connect(
        self,
        ip: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> tuple[Optional["paramiko.SSHClient"], int, str]:
        """建立並回傳已連線的 paramiko.SSHClient；失敗時回傳 (None, ERR_*, 錯誤訊息)"""
        if paramiko is None:
            return None, self.ERR_CONFIG, "尚未安裝 paramiko 套件，請執行 pip install paramiko"

        try:
            ipaddress.ip_address(ip.strip())
        except ValueError:
            return None, self.ERR_CONFIG, f"無效的 IP 位址格式: {ip}"
        host = ip.strip()
        user = username or self.username
        conn_timeout = timeout or self.timeout

        use_password = password is not None and password != ""
        if not use_password and not self.key_path.exists():
            return (
                None,
                self.ERR_CONFIG,
                f"找不到 SSH 私鑰檔案: {self.key_path}，且未提供初始登入密碼 (請確認 .env 的 SSH_PRIVATE_KEY_PATH 或 BOOTSTRAP_INIT_PASSWORD)",
            )

        client = paramiko.SSHClient()
        try:
            client.load_system_host_keys()
            if self.strict_host_key:
                client.set_missing_host_key_policy(paramiko.RejectPolicy())
            else:
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            connect_kwargs: dict = {
                "hostname": host,
                "port": self.port,
                "username": user,
                "timeout": conn_timeout,
                "banner_timeout": conn_timeout,
                "auth_timeout": conn_timeout,
                "look_for_keys": False,
                "allow_agent": False,
            }
            if use_password:
                connect_kwargs["password"] = password
            else:
                connect_kwargs["key_filename"] = str(self.key_path)
                connect_kwargs["passphrase"] = self.passphrase

            client.connect(**connect_kwargs)
            return client, 0, ""

        except paramiko.AuthenticationException as e:
            client.close()
            auth_mode = "密碼" if use_password else "SSH 金鑰"
            return None, self.ERR_AUTH, f"SSH {auth_mode}驗證失敗 ({user}@{host})：{e}"
        except paramiko.SSHException as e:
            client.close()
            if isinstance(e, paramiko.PasswordRequiredException):
                return None, self.ERR_AUTH, f"SSH 私鑰需要密碼 (請設定 SSH_KEY_PASSPHRASE)：{e}"
            return None, self.ERR_SSH, f"SSH 協定錯誤 ({host})：{e}"
        except (socket.timeout, TimeoutError) as e:
            client.close()
            return None, self.ERR_TIMEOUT, f"SSH 連線逾時 ({conn_timeout}s)：{host} - {e}"
        except (paramiko.ssh_exception.NoValidConnectionsError, ConnectionError, OSError) as e:
            client.close()
            return None, self.ERR_UNREACHABLE, f"主機不可達或連線被拒：{host}:{self.port} - {e}"
        except Exception as e:
            client.close()
            return None, self.ERR_SSH, f"SSH 連線發生未預期錯誤 ({host})：{type(e).__name__}: {e}"

    def _handle_pam_expired_password(
        self,
        ip: str,
        username: str,
        old_password: str,
        new_password: str,
    ) -> tuple[bool, str]:
        """
        處理新 VM 模板首次 SSH 連線時因密碼過期 (PAM password expired) 強制要求立即變更密碼的情境。
        同時相容：
          1. Keyboard-Interactive 認證階段要求輸入 Current / New / Retype password
          2. 密碼認證通過後，開啟 PTY Shell 時由 PAM 互動提示輸入 Current / New / Retype password
        """
        host = ip.strip()
        transport: Optional["paramiko.Transport"] = None
        try:
            sock = socket.create_connection((host, self.port), timeout=self.timeout)
            transport = paramiko.Transport(sock)
            transport.start_client(timeout=self.timeout)

            changed_in_kbd = False

            def _kbd_handler(title: str, instructions: str, prompt_list: list[tuple[str, bool]]) -> list[str]:
                nonlocal changed_in_kbd
                answers: list[str] = []
                for prompt_text, _echo in prompt_list:
                    p = prompt_text.strip().lower()
                    if any(k in p for k in ("new password", "retype", "repeat", "confirm", "新密碼", "再次輸入")):
                        changed_in_kbd = True
                        answers.append(new_password)
                    else:
                        answers.append(old_password)
                return answers

            # 先嘗試 keyboard-interactive，若伺服器不支援或失敗則嘗試 password auth
            try:
                transport.auth_interactive(username, _kbd_handler)
            except paramiko.SSHException:
                if not transport.is_authenticated():
                    transport.auth_password(username, old_password)

            if not transport.is_authenticated():
                return False, "認證未通過"

            if changed_in_kbd:
                return True, "已於 SSH keyboard-interactive 階段完成首次強制改密碼"

            # 開啟 PTY Session 處理登入後 PAM 強制改密碼提示
            chan = transport.open_session(timeout=self.timeout)
            chan.get_pty(width=120, height=40)
            chan.invoke_shell()

            buf = ""
            sent_current = False
            sent_new = 0
            deadline = time.time() + 15.0

            while time.time() < deadline:
                if chan.recv_ready():
                    chunk = chan.recv(4096).decode("utf-8", errors="replace")
                    buf += chunk
                    clean_tail = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", buf[-300:]).strip().lower()

                    if any(
                        msg in clean_tail
                        for msg in (
                            "all authentication tokens updated successfully",
                            "password updated successfully",
                        )
                    ):
                        chan.close()
                        return True, "已於 PTY Shell 完成首次登入強制改密碼"

                    # 1. 要求輸入目前(舊)密碼
                    if not sent_current and re.search(
                        r"(\(current\)\s*unix\s*password|current\s*password|old\s*password|目前.*密碼)\s*[:：]\s*$",
                        clean_tail,
                    ):
                        chan.send(old_password + "\n")
                        sent_current = True
                        buf = ""
                        continue

                    # 2. 要求再次輸入新密碼 (Retype / Confirm)
                    if re.search(
                        r"(retype\s*new\s*password|confirm\s*new\s*password|repeat\s*new\s*password|再次輸入.*新密碼)\s*[:：]\s*$",
                        clean_tail,
                    ):
                        chan.send(new_password + "\n")
                        sent_new += 1
                        buf = ""
                        continue

                    # 3. 要求輸入新密碼 (New password)
                    if re.search(r"(new\s*password|新密碼)\s*[:：]\s*$", clean_tail):
                        chan.send(new_password + "\n")
                        sent_new += 1
                        buf = ""
                        continue

                    # 若已經出現一般 Shell Prompt ($ 或 #) 且無 expired 字眼，代表其實沒過期
                    if sent_new == 0 and not sent_current and re.search(r"[\$#]\s*$", clean_tail) and "expired" not in clean_tail:
                        chan.close()
                        return False, "未出現強制改密碼提示"
                else:
                    if chan.closed or chan.exit_status_ready():
                        break
                    time.sleep(0.15)

            chan.close()
            if sent_new >= 2:
                return True, "已送出新密碼完成首次強制改密碼"
            return False, f"PAM 互動改密碼未完成，最後輸出：{buf[-200:].strip()}"

        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
        finally:
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass

    def ensure_initial_login(
        self,
        ip: str,
        username: str,
        init_password: Optional[str],
        new_password: str,
        os_type: str = "linux",
    ) -> tuple[int, Optional[str], bool, str]:
        """
        確保目標新 VM 可正常執行指令與 SFTP，若遇首次登入強制改密碼 (Linux PAM) 則自動改為 new_password。

        Returns:
            (exit_code, active_password, password_changed_by_pam, status_message)
            - exit_code == 0 代表連線就緒
            - active_password 為後續 SFTP / sudo 應使用的密碼 (若為 None 代表使用 SSH 私鑰連線)
            - password_changed_by_pam 為 True 代表剛剛已自動完成 PAM 首次強制改密碼
        """
        is_windows = (os_type or "linux").strip().lower() == "windows"
        exec_kw = {"os_type": "windows"} if is_windows else {}

        # 1. 若有提供初始密碼，先測試直接執行 whoami
        if init_password:
            ec, out, err = self.exec_command(
                ip, "whoami", username=username, password=init_password, cmd_timeout=8, **exec_kw
            )
            combined = f"{out}\n{err}".lower()
            if ec == 0 and username.lower() in out.lower() and "expired" not in combined and "change your password" not in combined:
                return 0, init_password, False, "使用初始預設密碼連線正常"

            # 2. Linux 若因密碼過期導致 exec_command 失敗或出現 expired 提示，啟動 PAM 互動改密碼
            if not is_windows:
                changed, msg = self._handle_pam_expired_password(ip, username, init_password, new_password)
                if changed:
                    ec_new, out_new, err_new = self.exec_command(
                        ip, "whoami", username=username, password=new_password, cmd_timeout=8
                    )
                    if ec_new == 0 and username.lower() in out_new.lower():
                        return 0, new_password, True, msg
                    return (
                        self.ERR_AUTH,
                        None,
                        True,
                        f"已執行 PAM 改密碼 ({msg})，但以新密碼驗證失敗：{err_new or out_new}",
                    )

        # 3. 嘗試是否已經是 new_password 或已配置 SSH 私鑰
        if new_password:
            ec2, out2, _ = self.exec_command(
                ip, "whoami", username=username, password=new_password, cmd_timeout=6, **exec_kw
            )
            if ec2 == 0 and username.lower() in out2.lower():
                return 0, new_password, False, "已使用新密碼連線成功"

        if self.key_path.exists():
            ec3, out3, err3 = self.exec_command(
                ip, "whoami", username=username, password=None, cmd_timeout=6, **exec_kw
            )
            if ec3 == 0 and username.lower() in out3.lower():
                return 0, None, False, "已使用 SSH 私鑰免密連線成功"
            if not init_password:
                return ec3, None, False, err3

        return self.ERR_AUTH, None, False, f"無法以初始密碼或私鑰登入 {username}@{ip} (請確認 BOOTSTRAP_INIT_PASSWORD 是否正確)"

    @staticmethod
    def _to_sftp_path(path: str, os_type: str = "linux") -> str:
        """將遠端路徑正規化為 SFTP 協定格式 (Windows OpenSSH SFTP 絕對路徑為 /C:/...)"""
        if (os_type or "linux").strip().lower() != "windows":
            return path
        norm = path.replace("\\", "/").rstrip("/")
        if re.match(r"^[A-Za-z]:(/|$)", norm):
            norm = "/" + norm
        return norm

    @staticmethod
    def _to_win_path(path: str) -> str:
        """將 SFTP 或正斜線 Windows 路徑轉回原生反斜線路徑 (/C:/Temp/a.ps1 -> C:\\Temp\\a.ps1)"""
        norm = path.replace("\\", "/")
        if re.match(r"^/[A-Za-z]:(/|$)", norm):
            norm = norm[1:]
        return norm.replace("/", "\\")

    @staticmethod
    def _ps_quote(value: str) -> str:
        """PowerShell 單引號字串跳脫"""
        return "'" + str(value).replace("'", "''") + "'"

    @staticmethod
    def build_env_file(secret_env: dict[str, str]) -> str:
        """產生可被 bash `source` 的 KEY='value' 內容 (值以 shlex.quote 跳脫)"""
        lines = []
        for k, v in secret_env.items():
            if v is None:
                continue
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
                raise ValueError(f"不合法的環境變數名稱: {k}")
            lines.append(f"{k}={shlex.quote(str(v))}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def build_windows_secret_file(secret_env: dict[str, str]) -> str:
        """產生給 Windows PowerShell 載入的 UTF-8 JSON 機密環境變數內容"""
        clean: dict[str, str] = {}
        for k, v in secret_env.items():
            if v is None:
                continue
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
                raise ValueError(f"不合法的環境變數名稱: {k}")
            clean[k] = str(v)
        return json.dumps(clean, ensure_ascii=False)

    @classmethod
    def build_windows_command(
        cls,
        cmd: str,
        env: Optional[dict[str, str]] = None,
        env_file: Optional[str] = None,
    ) -> str:
        """
        組合送往 Windows OpenSSH (預設 Shell 為 cmd.exe) 的遠端指令。
        - 純粹的 cmd 指令 (無 env / env_file 且非 .ps1 腳本) 直接送出 (如 whoami / shutdown)。
        - .ps1 腳本或含環境變數時，透過 PowerShell `-EncodedCommand` 避開 cmd.exe 跳脫地雷；
          機密變數絕不放入命令列或 EncodedCommand，而是從 SFTP 暫存 JSON 檔載入後立即刪除。
        """
        stripped = cmd.strip()
        is_ps1 = stripped.lower().endswith(".ps1")
        if not env and not env_file and not is_ps1:
            return cmd

        ps_lines = [
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
            "$OutputEncoding = [System.Text.Encoding]::UTF8",
        ]
        for k, v in (env or {}).items():
            if v is None:
                continue
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
                raise ValueError(f"不合法的環境變數名稱: {k}")
            ps_lines.append(f"$env:{k} = {cls._ps_quote(str(v))}")

        if env_file:
            win_sf = cls._to_win_path(env_file)
            ps_lines.extend([
                f"$__sf = {cls._ps_quote(win_sf)}",
                "if (Test-Path -LiteralPath $__sf) {",
                "  try {",
                "    $__d = Get-Content -LiteralPath $__sf -Raw -Encoding UTF8 | ConvertFrom-Json",
                "    foreach ($__p in $__d.PSObject.Properties) {",
                "      Set-Item -Path ('Env:' + $__p.Name) -Value ([string]$__p.Value)",
                "    }",
                "  } finally {",
                "    Remove-Item -LiteralPath $__sf -Force -ErrorAction SilentlyContinue",
                "  }",
                "}",
            ])

        invoke_line = f"& {cls._ps_quote(cls._to_win_path(stripped))}" if is_ps1 else stripped
        ps_lines.extend([
            "try {",
            "  $global:LASTEXITCODE = 0",
            f"  {invoke_line}",
            "  if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
            "  if (-not $?) { exit 1 }",
            "  exit 0",
            "} catch {",
            "  [Console]::Error.WriteLine($_.Exception.Message)",
            "  exit 1",
            "}",
        ])

        script = "\n".join(ps_lines)
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return f"powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {encoded}"

    @staticmethod
    def build_command(
        cmd: str,
        env: Optional[dict[str, str]] = None,
        sudo: bool = False,
        sudo_password: bool = False,
        env_file: Optional[str] = None,
    ) -> str:
        """
        組合實際送出的遠端指令 (Linux POSIX / bash)。
        - env：非機密環境變數，直接放在命令列 (遠端 ps 可見)。
        - env_file：機密環境變數檔路徑；載入後立即刪除，再執行 cmd，機密值不會出現在命令列。
        """
        env_prefix = ""
        if env:
            env_pairs = [f"{k}={shlex.quote(str(v))}" for k, v in env.items() if v is not None]
            if env_pairs:
                env_prefix = " ".join(env_pairs) + " "

        inner = cmd
        if env_file:
            f = shlex.quote(env_file)
            inner = f"set -a; . {f}; set +a; rm -f {f}; {cmd}"

        if sudo:
            flag = "-S -p ''" if sudo_password else "-n"
            return f"sudo {flag} env {env_prefix}bash -c {shlex.quote(inner)}"
        if env_file:
            return f"{env_prefix}bash -c {shlex.quote(inner)}"
        return f"{env_prefix}{cmd}"

    def exec_command(
        self,
        ip: str,
        cmd: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        sudo: bool = False,
        sudo_password: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        cmd_timeout: Optional[int] = None,
        secret_env: Optional[dict[str, str]] = None,
        os_type: str = "linux",
        remote_temp_dir: Optional[str] = None,
    ) -> tuple[int, str, str]:
        """
        連入 ip 並執行 cmd (支援 Linux 與 Windows OpenSSH、私鑰或密碼登入、sudo -S 提權與機密環境變數注入)。

        secret_env：機密環境變數 (密碼、Token)。會先以 SFTP 寫入暫存檔 (Linux 權限 600 / Windows 暫存 JSON)，
                    遠端 shell 載入後立即刪除，避免出現在 `ps`、sudo log 或命令列紀錄中。

        Returns:
            (exit_code, stdout, stderr)
            exit_code >= 0 為遠端指令回傳值；< 0 為本地端連線錯誤 (見 ERR_* 常數)，錯誤說明放在 stderr。
        """
        is_windows = (os_type or "linux").strip().lower() == "windows"
        exec_timeout = cmd_timeout or self.timeout
        client, err_code, err_msg = self._connect(ip, username=username, password=password)
        if client is None:
            return err_code, "", err_msg

        env_file: Optional[str] = None
        try:
            if secret_env:
                sftp = client.open_sftp()
                try:
                    if is_windows:
                        base_tmp = self._to_sftp_path(remote_temp_dir or "C:/Temp/vm_bootstrap", os_type="windows")
                        self._sftp_mkdir_p(sftp, base_tmp)
                        env_file = posixpath.join(base_tmp, f".aria_env_{uuid.uuid4().hex}.json")
                        payload = self.build_windows_secret_file(secret_env).encode("utf-8")
                        with sftp.open(env_file, "wb") as fh:
                            fh.write(payload)
                    else:
                        env_file = f"/tmp/.aria_env_{uuid.uuid4().hex}"
                        # 先建立空檔並設為 600，再寫入內容，避免短暫出現可被他人讀取的視窗
                        with sftp.open(env_file, "w") as fh:
                            sftp.chmod(env_file, 0o600)
                            fh.write(self.build_env_file(secret_env))
                finally:
                    sftp.close()

            if is_windows:
                full_cmd = self.build_windows_command(cmd, env=env, env_file=env_file)
            else:
                full_cmd = self.build_command(
                    cmd, env=env, sudo=sudo, sudo_password=bool(sudo and sudo_password), env_file=env_file
                )

            stdin, stdout, stderr = client.exec_command(full_cmd, timeout=exec_timeout)
            if not is_windows and sudo and sudo_password:
                stdin.write(sudo_password + "\n")
                stdin.flush()
            stdin.channel.shutdown_write()

            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            exit_code = stdout.channel.recv_exit_status()
            return exit_code, out, err

        except (socket.timeout, TimeoutError) as e:
            return self.ERR_TIMEOUT, "", f"SSH 指令執行逾時 ({exec_timeout}s)：{ip} - {e}"
        except Exception as e:
            return self.ERR_SSH, "", f"SSH 指令執行錯誤 ({ip})：{type(e).__name__}: {e}"
        finally:
            if env_file:
                # 正常情況下遠端 shell 已刪除；若指令未執行到 (例如啟動失敗)，在此補刪
                try:
                    sftp = client.open_sftp()
                    try:
                        sftp.remove(env_file)
                    except OSError:
                        pass
                    finally:
                        sftp.close()
                except Exception:
                    pass
            client.close()

    @staticmethod
    def _sftp_mkdir_p(sftp: "paramiko.SFTPClient", remote_dir: str) -> None:
        """遞迴建立遠端目錄 (相容 Linux 路徑與 Windows /C:/... 路徑)"""
        dirs: list[str] = []
        cur = remote_dir.replace("\\", "/").rstrip("/")
        while cur not in ("", "/") and not re.match(r"^/?[A-Za-z]:$", cur):
            dirs.append(cur)
            parent = posixpath.dirname(cur)
            if parent == cur:
                break
            cur = parent
        for d in reversed(dirs):
            try:
                sftp.stat(d)
            except OSError:
                sftp.mkdir(d)

    @staticmethod
    def _put_file_normalized(sftp: "paramiko.SFTPClient", local_file: Path, remote_path: str) -> None:
        """上傳單一檔案：.sh 轉 LF + 0755；.ps1 轉 UTF-8 BOM + CRLF (相容 PowerShell 5.1)"""
        suffix = local_file.suffix.lower()
        if suffix == ".sh":
            raw = local_file.read_bytes().replace(b"\r\n", b"\n")
            sftp.putfo(io.BytesIO(raw), remote_path)
            sftp.chmod(remote_path, 0o755)
        elif suffix == ".ps1":
            text = local_file.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\n", "\r\n")
            sftp.putfo(io.BytesIO(text.encode("utf-8-sig")), remote_path)
        else:
            sftp.put(str(local_file), remote_path)

    def wait_port_ready(self, ip: str, initial_delay: float = 3.0, timeout: float = 20.0) -> bool:
        """等待遠端 SSH 服務完成重啟並重新接受 TCP 連線"""
        host = ip.strip()
        if initial_delay > 0:
            time.sleep(initial_delay)
        deadline = time.time() + max(timeout, 1.0)
        while time.time() < deadline:
            try:
                with socket.create_connection((host, self.port), timeout=2.0):
                    return True
            except OSError:
                time.sleep(1.0)
        return False

    def upload_files(
        self,
        ip: str,
        files: list[Path],
        remote_dir: str = "/tmp",
        extra_dirs: Optional[list[tuple[Path, str]]] = None,
        extra_files: Optional[list[tuple[Path, str]]] = None,
        exclude_dir_names: Optional[set[str]] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        transfer_timeout: int = 120,
        os_type: str = "linux",
    ) -> tuple[int, list[str], str]:
        """
        透過 SFTP 批次上傳腳本檔案、指定檔案清單 (extra_files) 與附加資料夾 (extra_dirs)。
        - exclude_dir_names：於遞迴 extra_dirs 時略過的資料夾名稱 (不分大小寫，例如 {"i386"})。
        - 附加安裝檔若遠端已存在且大小一致會略過重傳 (利於中斷續跑)。

        Returns:
            (0, uploaded_remote_paths, "") 成功；或 (ERR_*, uploaded_so_far, err_msg) 失敗。
        """
        client, err_code, err_msg = self._connect(
            ip, username=username, password=password, timeout=transfer_timeout
        )
        if client is None:
            return err_code, [], err_msg

        sftp_remote_dir = self._to_sftp_path(remote_dir, os_type=os_type)
        excluded = {d.lower() for d in (exclude_dir_names or set())}
        uploaded: list[str] = []
        try:
            sftp = client.open_sftp()
            try:
                self._sftp_mkdir_p(sftp, sftp_remote_dir)
                for f in files:
                    if not f.is_file():
                        return self.ERR_CONFIG, uploaded, f"找不到本機腳本檔案: {f}"
                    rpath = posixpath.join(sftp_remote_dir, f.name)
                    self._put_file_normalized(sftp, f, rpath)
                    uploaded.append(rpath)

                for local_file, target_remote_dir in extra_files or []:
                    if not local_file.is_file():
                        continue
                    sftp_target_dir = self._to_sftp_path(target_remote_dir, os_type=os_type)
                    self._sftp_mkdir_p(sftp, sftp_target_dir)
                    rtarget = posixpath.join(sftp_target_dir, local_file.name)
                    if local_file.suffix.lower() not in (".sh", ".ps1"):
                        try:
                            if sftp.stat(rtarget).st_size == local_file.stat().st_size:
                                uploaded.append(rtarget)
                                continue
                        except OSError:
                            pass
                    self._put_file_normalized(sftp, local_file, rtarget)
                    uploaded.append(rtarget)

                for local_folder, target_remote_dir in extra_dirs or []:
                    if not local_folder.is_dir():
                        continue
                    sftp_target_dir = self._to_sftp_path(target_remote_dir, os_type=os_type)
                    self._sftp_mkdir_p(sftp, sftp_target_dir)
                    for item in local_folder.rglob("*"):
                        rel_parts = item.relative_to(local_folder).parts
                        if excluded and any(part.lower() in excluded for part in rel_parts):
                            continue
                        rel = item.relative_to(local_folder).as_posix()
                        rtarget = posixpath.join(sftp_target_dir, rel)
                        if item.is_dir():
                            self._sftp_mkdir_p(sftp, rtarget)
                        elif item.is_file():
                            self._sftp_mkdir_p(sftp, posixpath.dirname(rtarget))
                            if item.suffix.lower() not in (".sh", ".ps1"):
                                try:
                                    if sftp.stat(rtarget).st_size == item.stat().st_size:
                                        uploaded.append(rtarget)
                                        continue
                                except OSError:
                                    pass
                            self._put_file_normalized(sftp, item, rtarget)
                            uploaded.append(rtarget)
            finally:
                sftp.close()
            return 0, uploaded, ""
        except Exception as e:
            return self.ERR_SSH, uploaded, f"SFTP 上傳失敗 ({ip})：{type(e).__name__}: {e}"
        finally:
            client.close()


@lru_cache(maxsize=1)
def get_ssh_client() -> SSHClient:
    """取得共用的 SSH 執行器 (測試時可用 get_ssh_client.cache_clear() 或 monkeypatch 替換)"""
    return SSHClient()

