"""Diff cloud-side and bridge-side device sets into four categories.

This is pure logic with no I/O — it operates on already-parsed Device dicts.
The same function powers both the CLI dashboard and the future web UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Device


@dataclass
class DiffResult:
    synced: list[Device] = field(default_factory=list)
    mismatched: list[tuple[Device, list[str]]] = field(default_factory=list)
    missing: list[Device] = field(default_factory=list)  # in cloud, not in bridge
    orphaned: list[Device] = field(default_factory=list)  # in bridge, not in cloud

    @property
    def has_changes(self) -> bool:
        return bool(self.mismatched or self.missing or self.orphaned)

    def summary(self) -> str:
        return (
            f"{len(self.synced)} synced, {len(self.mismatched)} mismatch, "
            f"{len(self.missing)} missing, {len(self.orphaned)} orphaned"
        )


def diff(
    cloud: dict[str, Device],
    bridge: dict[str, Device],
) -> DiffResult:
    result = DiffResult()
    cloud_ids = set(cloud.keys())
    bridge_ids = set(bridge.keys())

    for cid in cloud_ids:
        cdev = cloud[cid]
        if cid not in bridge_ids:
            result.missing.append(cdev)
            continue
        mismatches = cdev.compare(bridge[cid])
        if mismatches:
            result.mismatched.append((cdev, mismatches))
        else:
            result.synced.append(cdev)

    for bid in bridge_ids - cloud_ids:
        result.orphaned.append(bridge[bid])

    return result
