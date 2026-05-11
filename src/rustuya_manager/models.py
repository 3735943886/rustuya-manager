"""Domain model for a Tuya device, used on both the cloud side and the bridge side.

A single `Device` shape lets us compare cloud-of-record (`tuyadevices.json`) entries
against bridge-reported entries without per-source branching elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Device:
    id: str
    name: str = "N/A"
    type: str = "WiFi"
    cid: str | None = None
    parent_id: str | None = None
    key: str | None = None
    ip: str = "Auto"
    version: str = "Auto"
    status: str = "offline"
    raw_data: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Device:
        did = data["id"]
        cid = data.get("node_id") or data.get("cid")
        parent_id = data.get("parent") or data.get("parent_id")
        ip = data.get("ip") or "Auto"

        # A device is treated as a sub-device when it advertises a cid (or sub flag)
        # AND it has no direct IP. Cloud entries with explicit IP override this.
        is_sub = (data.get("sub") is True) or (cid is not None)
        if ip != "Auto" and not parent_id:
            is_sub = False

        return cls(
            id=did,
            name=data.get("name", "N/A"),
            type="SubDevice" if is_sub else "WiFi",
            cid=cid,
            parent_id=parent_id,
            key=data.get("local_key") or data.get("key"),
            ip=ip,
            version=data.get("version") or data.get("ver") or "Auto",
            status=str(data.get("status", "offline")),
            raw_data=data,
        )

    @staticmethod
    def shorten(val: str | None, length: int = 12) -> str:
        if not val or len(val) <= length:
            return str(val)
        return f"{val[:4]}...{val[-4:]}"

    def routing_info(self) -> str:
        if self.type == "SubDevice":
            return f"P:{self.shorten(self.parent_id)} C:{self.cid}"
        return ""

    def compare(self, other: Device) -> list[str]:
        """Returns a human-readable list of field mismatches between `self`
        (cloud authority) and `other` (bridge state). Empty list means match."""
        mismatches: list[str] = []
        if self.type == "WiFi":
            if self.key and other.key and self.key != other.key:
                mismatches.append(
                    f"KEY: {self.shorten(other.key)} -> {self.shorten(self.key)}"
                )
            if other.ip != "Auto" and self.ip != other.ip:
                mismatches.append(f"IP: {other.ip} -> {self.ip}")
            if other.version != "Auto" and self.version != other.version:
                mismatches.append(f"VER: {other.version} -> {self.version}")
        else:
            if self.cid != other.cid:
                mismatches.append(f"CID: {other.cid} -> {self.cid}")
            if self.parent_id != other.parent_id:
                mismatches.append(
                    f"PARENT: {self.shorten(other.parent_id)} -> {self.shorten(self.parent_id)}"
                )
        return mismatches
