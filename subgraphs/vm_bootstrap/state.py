"""VM Bootstrap 子圖的 State"""
from typing import Annotated, Any, Optional

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class BootstrapState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    ip: str
    hostname: Optional[str]
    cs_mode: str                  # "NGAV" | "MDR" | "SKIP"
    skip_disk: bool               # 是否略過磁碟擴容與掛載腳本
    init_user: str                # 初始登入帳號 (預設 user1)
    user1: str                    # 管理帳號 (預設 user1，與 root 共用密碼)
    user2: str                    # 第二個固定帳號 (預設 user2)
    # 密碼參照：admin_password / user2_password 實際存放於 core.secrets.SecretStore (只在記憶體)，
    # state 與 checkpoint 中只保存參照 ID，不保存明文
    secret_ref: Optional[str]
    human_feedback: Optional[dict[str, Any]]
    validation_errors: list[str]
    # 目前 SSH 登入方式："init" (範本初始密碼) | "admin" (新產生的管理密碼) | "key" (SSH 私鑰)
    login_mode: str
    credentials_saved: bool
    credentials_location: Optional[str]
    # 步驟引擎進度 (每完成一步即寫入 checkpoint，可從中斷處續跑)
    step_index: int
    step_results: list[dict[str, Any]]
    stopped: bool
    execution_result: Optional[dict[str, Any]]
