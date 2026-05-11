"""Tests for the Device data model, particularly is_sub classification.

The rule (per user spec, verified against actual device inventory): a device
is a sub-device iff it has a non-empty `node_id`/`cid` AND no `ip`. The
cloud's `sub` flag is unreliable and ignored.
"""

from __future__ import annotations

from rustuya_manager.models import Device


class TestIsSubClassification:
    def test_cid_present_no_ip_is_sub(self):
        d = Device.from_dict({"id": "x", "node_id": "abc"})
        assert d.type == "SubDevice"

    def test_cid_present_empty_ip_is_sub(self):
        d = Device.from_dict({"id": "x", "node_id": "abc", "ip": ""})
        assert d.type == "SubDevice"

    def test_cid_present_with_ip_is_wifi(self):
        # cid + explicit ip → the device is reachable on the LAN directly,
        # so we treat it as a WiFi device even if it has a cid.
        d = Device.from_dict({"id": "x", "node_id": "abc", "ip": "192.168.1.10"})
        assert d.type == "WiFi"

    def test_no_cid_is_wifi(self):
        d = Device.from_dict({"id": "x", "ip": ""})
        assert d.type == "WiFi"

    def test_no_cid_with_ip_is_wifi(self):
        d = Device.from_dict({"id": "x", "ip": "1.2.3.4"})
        assert d.type == "WiFi"

    def test_sub_flag_alone_is_ignored(self):
        # The cloud's `sub` flag is unreliable. Without cid we don't treat
        # the device as a sub.
        d = Device.from_dict({"id": "x", "sub": True})
        assert d.type == "WiFi"

    def test_cid_via_bridge_key_name(self):
        # Bridge JSON uses "cid" instead of "node_id"; both should classify
        # the device the same way.
        d = Device.from_dict({"id": "x", "cid": "abc"})
        assert d.type == "SubDevice"

    def test_empty_cid_is_not_sub(self):
        d = Device.from_dict({"id": "x", "node_id": "", "ip": ""})
        assert d.type == "WiFi"

    def test_explicit_auto_ip_is_not_sub(self):
        # "Auto" as an explicit ip value is distinct from missing/empty —
        # the device is registered with the bridge in auto-discovery mode,
        # not as a sub. The is_sub rule looks at the raw ip before
        # normalization, so "Auto" stays WiFi.
        d = Device.from_dict({"id": "x", "node_id": "abc", "ip": "Auto"})
        assert d.type == "WiFi"
        # And the stored ip is preserved as "Auto" (the normalization
        # only kicks in when ip is missing/empty).
        assert d.ip == "Auto"
