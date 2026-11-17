"""VM 硬碟調整 (Day-2：Resize Boot Disk / Add Disk) 子圖"""
from subgraphs.vm_disk.graph import build_vm_disk_graph
from subgraphs.vm_disk.tool import adjust_vm_disk

__all__ = ["build_vm_disk_graph", "adjust_vm_disk"]
