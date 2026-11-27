"""Day-2 硬碟動作共用輔助：定位機器、組合動作、冪等檢查與單次狀態查詢"""
from typing import Any, Optional

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


def is_extra_disk_already_created(aria: Any, deployment_id: str, disk_prefix: str) -> bool:
    """冪等保險：送出 Add Disk 前，檢查 deployment 中是否已存在同名磁碟資源"""
    if not deployment_id or not disk_prefix:
        return False
    try:
        resources = aria.deployments.resources(deployment_id)
    except Exception:
        return False
    for r in resources or ():
        rtype = (r.get("type") or "").lower()
        if "disk" not in rtype and "volume" not in rtype:
            continue
        props = r.get("properties") or {}
        rname = str(props.get("resourceName") or r.get("name") or "")
        if rname.startswith(disk_prefix):
            return True
    return False


def poll_disk_op_once(
    aria: Any,
    ops: list[dict[str, Any]],
    target_idx: int,
) -> tuple[str, list[dict[str, Any]], str]:
    """
    對 Aria 查詢一次指定硬碟動作的 Request 狀態。
    回傳 (outcome, updated_ops, message)：
    - outcome: "success" | "failed" | "running"
    """
    updated = [dict(op) for op in ops]
    op = updated[target_idx]
    req = aria.deployments.get_request(op["request_id"])
    status = str(req.get("status") or "").upper()

    if status in OK_STATUSES:
        op["status"] = "success"
        return "success", updated, f"已完成：{op['label']}"

    if status in FAIL_STATUSES:
        detail = req.get("details") or status
        op.update(status="failed", error=detail)
        return "failed", updated, f"{op['label']} 失敗：{detail}"

    return "running", updated, f"{op['label']} 執行中 ({status or '未知狀態'})"
