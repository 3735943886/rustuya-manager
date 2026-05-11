"""Unit tests for cloud.py helpers and the /api/cloud upload endpoint."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import paho.mqtt.client as mqtt
import pytest
from fastapi.testclient import TestClient

from rustuya_manager.cloud import CloudFormatError, load_cloud_file, parse_cloud_json, save_cloud_json
from rustuya_manager.mqtt import BridgeClient
from rustuya_manager.state import BridgeTemplates, State
from rustuya_manager.web import build_app


SAMPLE_LIST = [
    {"id": "bf-aaaa", "name": "lamp", "local_key": "k1", "ip": "192.168.1.10"},
    {"id": "bf-bbbb", "name": "switch", "local_key": "k2", "ip": "192.168.1.11"},
]
SAMPLE_DICT = {d["id"]: d for d in SAMPLE_LIST}


class TestParse:
    def test_list_shape(self):
        devs = parse_cloud_json(json.dumps(SAMPLE_LIST))
        assert set(devs) == {"bf-aaaa", "bf-bbbb"}
        assert devs["bf-aaaa"].key == "k1"

    def test_dict_shape(self):
        devs = parse_cloud_json(json.dumps(SAMPLE_DICT))
        assert set(devs) == {"bf-aaaa", "bf-bbbb"}

    def test_invalid_json_raises(self):
        with pytest.raises(CloudFormatError):
            parse_cloud_json("not-json")

    def test_top_level_string_raises(self):
        with pytest.raises(CloudFormatError):
            parse_cloud_json('"hello"')

    def test_empty_list_raises(self):
        with pytest.raises(CloudFormatError):
            parse_cloud_json("[]")

    def test_entries_without_id_dropped(self):
        # Mixed: one valid entry, one without id — the valid one is kept
        raw = json.dumps([{"name": "no-id"}, {"id": "bf-x", "name": "ok"}])
        devs = parse_cloud_json(raw)
        assert set(devs) == {"bf-x"}


class TestSaveLoad:
    def test_round_trip(self, tmp_path: Path):
        path = tmp_path / "tuyadevices.json"
        save_cloud_json(json.dumps(SAMPLE_LIST), path)
        assert path.exists()
        devs = load_cloud_file(path)
        assert set(devs) == {"bf-aaaa", "bf-bbbb"}

    def test_invalid_input_is_not_persisted(self, tmp_path: Path):
        path = tmp_path / "tuyadevices.json"
        with pytest.raises(CloudFormatError):
            save_cloud_json("not-json", path)
        # File must not have been created — never persist invalid data
        assert not path.exists()


# ─────────────────────────────────────────────────────────────────────────────
# HTTP endpoint
# ─────────────────────────────────────────────────────────────────────────────

def _fixture(tmp_path: Path | None = None) -> tuple[State, BridgeClient]:
    state = State()
    state.templates = BridgeTemplates(
        root="rustuya",
        command="rustuya/command",
        event="rustuya/event/{type}/{id}",
        message="rustuya/{level}/{id}",
        scanner="rustuya/scanner",
        payload="{value}",
    )
    if tmp_path is not None:
        state.cloud_path = str(tmp_path / "tuyadevices.json")
    client = BridgeClient(broker="mqtt://localhost:1883", root="rustuya", state=state)
    paho_mock = MagicMock(spec=mqtt.Client)
    paho_mock.publish.return_value = MagicMock(rc=mqtt.MQTT_ERR_SUCCESS)
    client._client = paho_mock
    return state, client


class TestUploadEndpoint:
    def test_upload_populates_state(self):
        state, client = _fixture()
        with TestClient(build_app(state, client)) as tc:
            r = tc.post("/api/cloud", content=json.dumps(SAMPLE_LIST))
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is True
            assert body["count"] == 2
            assert body["persisted_to"] is None  # no cloud_path set
        assert set(state.cloud) == {"bf-aaaa", "bf-bbbb"}

    def test_upload_persists_when_path_known(self, tmp_path: Path):
        state, client = _fixture(tmp_path)
        with TestClient(build_app(state, client)) as tc:
            r = tc.post("/api/cloud", content=json.dumps(SAMPLE_LIST))
            assert r.status_code == 200
            body = r.json()
            assert body["persisted_to"] == state.cloud_path
        assert Path(state.cloud_path).exists()
        # Reloadable
        devs = load_cloud_file(Path(state.cloud_path))
        assert set(devs) == {"bf-aaaa", "bf-bbbb"}

    def test_empty_body_rejected(self):
        state, client = _fixture()
        with TestClient(build_app(state, client)) as tc:
            r = tc.post("/api/cloud", content="")
            assert r.status_code == 400

    def test_invalid_body_rejected(self):
        state, client = _fixture()
        with TestClient(build_app(state, client)) as tc:
            r = tc.post("/api/cloud", content="not json")
            assert r.status_code == 400


class TestStateSerialization:
    """Verify the new state fields (last_seen, cloud_path, cloud_loaded) reach
    the API."""

    def test_state_endpoint_exposes_new_fields(self, tmp_path: Path):
        state, client = _fixture(tmp_path)
        with TestClient(build_app(state, client)) as tc:
            body = tc.get("/api/state").json()
            assert "last_seen" in body
            assert body["cloud_path"] == state.cloud_path
            assert body["cloud_loaded"] is False  # nothing uploaded yet

    async def test_last_seen_stamped_on_dps_merge(self):
        state = State()
        await state.merge_dps("bf-x", {"1": True})
        assert "bf-x" in state.last_seen
        assert state.last_seen["bf-x"] > 0
