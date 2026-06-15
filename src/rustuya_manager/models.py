"""Domain model for a Tuya device, used on both the cloud side and the bridge side.

A single `Device` shape lets us compare cloud-of-record (`tuyadevices.json`) entries
against bridge-reported entries without per-source branching elsewhere.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any


def _is_lan_ip(value: str) -> bool:
    """True when `value` is a LAN-local IP the bridge can reach directly.

    Mirrors the bridge's `normalize_config`: private, loopback, link-local and
    unspecified addresses count as LAN; a public/WAN/NAT address does not. A
    non-IP string (hostname, the "Auto" sentinel) is left untouched — the caller
    keeps it. Used to drop a meaningless public cloud IP to auto-discovery.
    """
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return True
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified


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

        # Sub-device classification (per user spec, 2026-05-11, verified against
        # the full device inventory). Computed against the RAW ip value before
        # normalization, because "Auto" explicitly set in the JSON is NOT the
        # same as a missing/empty ip — only the latter is the sub indicator.
        #   - cid present and non-empty (from "node_id" in cloud JSON or "cid"
        #     in bridge JSON — both normalized into `cid` above), AND
        #   - the "ip" key is absent or its value is an empty string.
        # The cloud JSON's `sub` flag is intentionally ignored; the user
        # reports it's unreliable (sometimes true for non-subs).
        raw_ip = data.get("ip")
        is_sub = bool(cid) and not raw_ip

        # After classification, normalize ip for display/storage. A device with
        # raw_ip == "" or missing is shown as "Auto" by convention.
        ip = raw_ip or "Auto"

        # A public/external IP is meaningless for LAN-local Tuya control: the
        # cloud reports a device's NAT'd WAN address, not its LAN address. The
        # bridge already drops a non-private IP to auto-discovery
        # (normalize_config), so mirror that here — keeping it would only
        # surface a non-actionable IP "mismatch" against the bridge's
        # auto-resolved LAN IP (and Update couldn't fix it; the bridge would
        # just re-drop the WAN value). Classification above already ran on the
        # raw ip, so this never changes WiFi/sub.
        if ip != "Auto" and not _is_lan_ip(ip):
            ip = "Auto"

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
        if not val:
            return ""
        if len(val) <= length:
            return val
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
                mismatches.append(f"KEY: {self.shorten(other.key)} -> {self.shorten(self.key)}")
            # "Auto" is a wildcard on EITHER side: the cloud authority not
            # pinning an IP (self) means there's nothing to enforce, and the
            # bridge in auto-discovery (other) can resolve to anything. Only two
            # concrete, differing IPs are a real, actionable mismatch — this also
            # keeps a normalized-away public cloud IP (now "Auto") from showing a
            # phantom "-> Auto" diff against the bridge's discovered LAN IP.
            if self.ip != "Auto" and other.ip != "Auto" and self.ip != other.ip:
                mismatches.append(f"IP: {other.ip} -> {self.ip}")
            # Same "Auto"-is-wildcard rule as IP above: the cloud not pinning a
            # protocol version (common — cloud JSON often omits it) is not a
            # conflict with whatever version the bridge negotiated, and Update
            # couldn't push "Auto" anyway. Only two concrete, differing versions
            # are a real mismatch.
            if self.version != "Auto" and other.version != "Auto" and self.version != other.version:
                mismatches.append(f"VER: {other.version} -> {self.version}")
        else:
            if self.cid != other.cid:
                mismatches.append(f"CID: {other.cid} -> {self.cid}")
            if self.parent_id != other.parent_id:
                mismatches.append(
                    f"PARENT: {self.shorten(other.parent_id)} -> {self.shorten(self.parent_id)}"
                )
        return mismatches
