"""Tests for the local browser GUI helpers."""

from __future__ import annotations

import json

import agentcontract.gui as gui
from agentcontract.gui import (
    DEFAULT_GUI_ACTION,
    DEFAULT_GUI_CONTRACT,
    _safe_contract_filename,
    gate_payload,
    save_contract_payload,
    validate_contract_payload,
)


def test_gui_default_contract_validates():
    result = validate_contract_payload(DEFAULT_GUI_CONTRACT)
    assert result["ok"] is True
    assert result["contract"]["agent_id"] == DEFAULT_GUI_CONTRACT["agent_id"]


def test_gui_gate_payload_returns_decision():
    result = gate_payload(DEFAULT_GUI_CONTRACT, DEFAULT_GUI_ACTION)
    assert result["ok"] is True
    assert result["result"]["decision"] == "require_approval"


def test_gui_gate_reports_contract_errors():
    payload = {**DEFAULT_GUI_CONTRACT, "agent_id": ""}
    result = gate_payload(payload, DEFAULT_GUI_ACTION)
    assert result["ok"] is False
    assert result["stage"] == "contract"


def test_gui_save_contract_writes_pretty_json(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "SAFE_SAVE_ROOTS", (tmp_path.resolve(),))
    destination = tmp_path / "contract.json"
    result = save_contract_payload(DEFAULT_GUI_CONTRACT, str(destination))
    assert result["ok"] is True
    assert destination.exists()
    saved = json.loads(destination.read_text())
    assert saved["agent_id"] == DEFAULT_GUI_CONTRACT["agent_id"]


def test_gui_save_rejects_paths_outside_safe_roots(tmp_path, monkeypatch):
    safe_root = tmp_path / "safe"
    safe_root.mkdir()
    monkeypatch.setattr(gui, "SAFE_SAVE_ROOTS", (safe_root.resolve(),))
    destination = tmp_path / "outside-agentcontract.json"
    result = save_contract_payload(DEFAULT_GUI_CONTRACT, str(destination))
    assert result["ok"] is False
    assert not destination.exists()
    assert "Save path must be inside" in result["errors"][0]["msg"]


def test_gui_fallback_filename_sanitises_agent_id():
    assert _safe_contract_filename("../../evil agent") == "evil-agent.json"
