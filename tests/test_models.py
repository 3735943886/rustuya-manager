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


class TestIpNormalization:
    """A public/external cloud IP is meaningless for LAN control, so it's
    normalized to "Auto" (mirroring the bridge's normalize_config). A private
    LAN IP is preserved verbatim."""

    def test_public_ip_normalized_to_auto(self):
        d = Device.from_dict({"id": "x", "ip": "1.2.3.4"})
        assert d.ip == "Auto"
        assert d.type == "WiFi"  # classification unaffected (raw ip was present)

    def test_private_ip_preserved(self):
        for ip in ("192.168.1.10", "10.0.0.5", "172.16.3.4", "169.254.1.1"):
            assert Device.from_dict({"id": "x", "ip": ip}).ip == ip

    def test_public_ip_with_cid_stays_wifi(self):
        # is_sub runs on the raw (present) ip, so a public ip + cid is WiFi, and
        # the public ip is then normalized away.
        d = Device.from_dict({"id": "x", "node_id": "abc", "ip": "8.8.8.8"})
        assert d.type == "WiFi"
        assert d.ip == "Auto"


class TestCompareIpWildcard:
    """`compare` treats "Auto" as a wildcard on either side — only two
    concrete, differing IPs are a real mismatch."""

    def _wifi(self, ip):
        return Device.from_dict({"id": "x", "ip": ip})

    def test_cloud_auto_vs_bridge_concrete_no_mismatch(self):
        # cloud unpinned (Auto), bridge auto-discovered a LAN IP → no IP diff.
        cloud, bridge = self._wifi("Auto"), self._wifi("192.168.1.5")
        assert cloud.compare(bridge) == []

    def test_normalized_public_cloud_ip_no_phantom_mismatch(self):
        # The exact case this change targets: a public cloud IP becomes "Auto",
        # so it must not diff against the bridge's discovered LAN IP.
        cloud, bridge = self._wifi("8.8.8.8"), self._wifi("192.168.1.5")
        assert cloud.ip == "Auto"
        assert cloud.compare(bridge) == []

    def test_two_concrete_private_ips_still_mismatch(self):
        cloud, bridge = self._wifi("192.168.1.9"), self._wifi("192.168.1.5")
        assert cloud.compare(bridge) == ["IP: 192.168.1.5 -> 192.168.1.9"]

    def test_bridge_auto_no_mismatch(self):
        cloud, bridge = self._wifi("192.168.1.9"), self._wifi("Auto")
        assert cloud.compare(bridge) == []


class TestCompareVersionWildcard:
    """Version uses the same "Auto"-is-wildcard rule as IP."""

    def _dev(self, ver):
        return Device.from_dict({"id": "x", "ip": "192.168.1.5", "version": ver})

    def test_cloud_no_version_vs_bridge_concrete_no_mismatch(self):
        # cloud JSON omitted version (→ Auto); bridge negotiated 3.4 → not a diff.
        cloud = Device.from_dict({"id": "x", "ip": "192.168.1.5"})  # no version
        bridge = self._dev("3.4")
        assert cloud.version == "Auto"
        assert cloud.compare(bridge) == []

    def test_two_concrete_versions_still_mismatch(self):
        assert self._dev("3.3").compare(self._dev("3.4")) == ["VER: 3.4 -> 3.3"]


class TestRealWorldBalconyLightswitch:
    """The reported case: bridge has a private IP + concrete version; cloud has
    a public (NAT) IP + no version. After normalization + wildcard rules the
    device is fully synced — no phantom IP/VER mismatch."""

    def test_synced_no_phantom_mismatch(self):
        bridge = Device.from_dict(
            {
                "id": "ebe9f29529dc499ed4bptk",
                "ip": "10.10.129.151",
                "key": "D-v|+7#1ci2t?EV;",
                "name": "balcony_lightswitch",
                "status": "0",
                "version": "3.4",
            }
        )
        cloud = Device.from_dict(
            {
                "id": "ebe9f29529dc499ed4bptk",
                "ip": "121.167.240.111",
                "local_key": "D-v|+7#1ci2t?EV;",
                "name": "balcony_lightswitch",
                # no "version" key
            }
        )
        assert cloud.ip == "Auto"  # public cloud IP normalized away
        assert cloud.version == "Auto"  # cloud omitted version
        assert cloud.compare(bridge) == []  # → synced, not mismatch
