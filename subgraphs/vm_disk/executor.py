"""Day-2 硬碟動作：定位機器、組合動作、依序送出與追蹤

同一套 step() 同時給兩種情境使用：
- 追蹤工作：背景監看器每次輪詢呼叫一次 step()，不阻塞 CLI
- adjust_vm_disk 工具：run_until_done() 反覆呼叫 step() 直到完成 (同步)

同一台機器的 Day-2 動作依序執行 (前一個成功後才送出下一個)，避免互相衝突。
"""
import time
from typing import Any, Callable, Optional

ADD_DISK = "Cloud.vSphere.Machine.Add.Disk"
RESIZE_BOOT_DISK = "Cloud.vSphere.Machine.Compute.Disk.Resize"

OK_STATUSES = ("SUCCESSFUL", "COMPLETED")
FAIL_STATUSES = ("FAILED", "ABORTED", "APPROVAL_REJECTED", "REJECTED", "CANCELLED")


def extra_disk_name(vm_name: str) -> str:
    """額外硬碟命名：{VM名稱}-disk2- (Aria 會自動補上 6 位流水號)"""
    return f"{vm_name}-disk2-"


def locate_machine(aria: Any, deployment_id: Optional[str] = None, ip: Optional[str] = None) -> dict[str, Any]:
    """找出 Day-2 動作要用的 deployment machine resource (注意：與 IaaS machine ID 不同)"""
    if not deployment_id and ip:
        machines = aria.iaas.find_machines_by_ip(ip)
        if not machines:
            raise ValueError(f"Aria 中找不到 IP 為 {ip} 的機器")
        deployment_id = machines[0].get("deploymentId")
    if not deployment_id:
        raise ValueError("需要 deployment_id 或 IP 才能定位機器")
    machines = [r for r in aria.deployments.resources(deployment_id) if "machine" in (r.get("type") or "").lower()]
    if ip:
        machines = [r for r in machines if (r.get("properties") or {}).get("address") == ip] or machines
    if not machines:
        raise ValueError(f"Deployment {deployment_id} 中沒有機器資源 (可能尚未建立完成)")
    m = machines[0]
    props = m.get("properties") or {}
    return {
        "deployment_id": deployment_id,
        "machine_id": m.get("id"),
        "vm_name": props.get("resourceName") or m.get("name") or "",
        "ip": props.get("address") or ip or "",
    }


def build_ops(vm_name: str, boot_disk_gb: Optional[int], extra_disk_gb: Optional[int]) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = []
    if boot_disk_gb:
        ops.append({"action_id": RESIZE_BOOT_DISK, "label": f"系統碟擴充至 {boot_disk_gb} GB",
                    "inputs": {"capacityGb": int(boot_disk_gb)}})
    if extra_disk_gb:
        name = extra_disk_name(vm_name)
        ops.append({"action_id": ADD_DISK, "label": f"加掛額外硬碟 {extra_disk_gb} GB ({name}…)",
                    "inputs": {"name": name, "capacityGb": int(extra_disk_gb)}})
    for op in ops:
        op.update(status="pending", request_id=None, submitted_at=None, error="")
    return ops


def step(aria: Any, machine_id: str, ops: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str, str]:
    """
    推進一次：送出下一個待執行的動作，或查詢執行中動作的狀態。
    回傳 (ops, 狀態 running|done|failed, 說明)
    """
    ops = [dict(op) for op in ops]
    for op in ops:
        if op["status"] == "success":
            continue
        if op["status"] == "failed":
            return ops, "failed", f"{op['label']} 失敗：{op.get('error')}"
        if not op.get("request_id"):
            resp = aria.deployments.run_resource_action(machine_id, op["action_id"], op["inputs"],
                                                        reason="Aria Agent 硬碟調整")
            op.update(status="running", request_id=resp.get("id"), submitted_at=time.time())
            return ops, "running", f"已送出：{op['label']}"
        req = aria.deployments.get_request(op["request_id"])
        status = str(req.get("status") or "").upper()
        if status in OK_STATUSES:
            op["status"] = "success"
            continue
        if status in FAIL_STATUSES:
            op.update(status="failed", error=req.get("details") or status)
            return ops, "failed", f"{op['label']} 失敗：{op['error']}"
        return ops, "running", f"{op['label']} 執行中 ({status or '未知狀態'})"
    return ops, "done", "硬碟調整完成"


def run_until_done(aria: Any, machine_id: str, ops: list[dict[str, Any]], timeout_s: float,
                   poll_s: float = 10, sleep: Optional[Callable[[float], None]] = None,
                   on_progress: Optional[Callable[[str], None]] = None) -> tuple[list[dict[str, Any]], str, str]:
    deadline = time.time() + timeout_s
    last_msg = ""
    while True:
        ops, state, msg = step(aria, machine_id, ops)
        if msg != last_msg and on_progress:
            on_progress(msg)
        last_msg = msg
        if state != "running":
            return ops, state, msg
        if time.time() > deadline:
            return ops, "timeout", f"等待逾時：{msg}"
        (sleep or time.sleep)(poll_s)
