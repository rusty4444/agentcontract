"""Tests for the enforcement module."""
from __future__ import annotations


import pytest

from agentcontract.core import (
    ActionContext,
    Contract,
    GateDecision,
    GateResult,
    GateViolation,
)
from agentcontract.enforcement import (
    ContractEnforcer,
    ContractViolation,
)
from agentcontract.sdk import HermesMCPContext


# ── Fixture contracts ────────────────────────────────────────────────────────

OPEN = Contract(agent_id="open", allow_tools=None, deny_tools=[], allow_network=True)

DENY_SHELL = Contract(
    agent_id="deny-shell",
    allow_tools=None,
    deny_tools=["shell"],
    allow_network=True,
)

APPROVAL_ONLY = Contract(
    agent_id="approval",
    allow_tools=["shell", "read_file"],
    require_approval=["shell"],
    allow_network=True,
)

NO_NETWORK = Contract(agent_id="nonet", allow_network=False)

PATH_RESTRICTED = Contract(
    agent_id="path-lock",
    allow_paths=["/workspace/"],
    deny_paths=["/etc/"],
)


# ── Construction ─────────────────────────────────────────────────────────────

def test_from_contract_object():
    enforcer = ContractEnforcer(OPEN)
    assert enforcer.contract.agent_id == "open"
    assert "Contract" in repr(enforcer)


def test_from_file(tmp_path):
    f = tmp_path / "contract.json"
    f.write_text(OPEN.model_dump_json())
    e = ContractEnforcer(str(f))
    assert e.contract.agent_id == "open"


def test_from_yaml_file(tmp_path):
    pytest.importorskip("yaml")
    import yaml

    data = {"agent_id": "yaml-agent", "allow_tools": None, "deny_tools": [], "allow_network": True}
    f = tmp_path / "contract.yaml"
    f.write_text(yaml.dump(data))
    e = ContractEnforcer(str(f))
    assert e.contract.agent_id == "yaml-agent"


def test_from_raw_json_string():
    e = ContractEnforcer(OPEN.model_dump_json())
    assert e.contract.agent_id == "open"


def test_from_raw_json_dict_string():
    raw = OPEN.model_dump_json()
    e = ContractEnforcer(raw)
    assert e.contract.agent_id == "open"


def test_source_label_contract_object():
    e = ContractEnforcer(OPEN, source="my-source")
    assert "my-source" in e.source



def test_source_label_file(tmp_path):
    f = tmp_path / "c.json"
    f.write_text(OPEN.model_dump_json())
    e = ContractEnforcer(str(f), source="file-src")
    assert "file-src" in e.source



def test_reject_hot_swap_bad_contract(capsys):
    e = ContractEnforcer(OPEN)
    e.replace_contract('{"agent_id": "x", "version": "bob"}')
    captured = capsys.readouterr()
    assert "rejected" in captured.out
    assert e.contract.agent_id == "open"   # unchanged


def test_hot_swap_good_contract(tmp_path):
    f = tmp_path / "c2.json"
    f.write_text(DENY_SHELL.model_dump_json())
    e = ContractEnforcer(OPEN)
    e.replace_contract(str(f), source="swapped")
    assert e.contract.agent_id == "deny-shell"
    assert "swapped" in e.source
    assert e.audit_trail._records == []


def test_hot_swap_from_object():
    e = ContractEnforcer(OPEN)
    e.replace_contract(DENY_SHELL)
    assert e.contract.agent_id == "deny-shell"


# ── check() ─────────────────────────────────────────────────────────────────

def test_check_returns_gateresult():
    e = ContractEnforcer(OPEN)
    r = e.check("shell", requires_network=False)
    assert isinstance(r, GateResult)
    assert r.decision == GateDecision.ALLOW


def test_check_blocks_denied_tool():
    e = ContractEnforcer(DENY_SHELL)
    r = e.check("shell")
    assert r.decision == GateDecision.BLOCK
    assert r.violations[0].field == "deny_tools"


def test_check_blocks_tool_not_in_allowlist():
    c = Contract(agent_id="wlist", allow_tools=["rf"])
    e = ContractEnforcer(c)
    r = e.check("http_get")
    assert r.decision == GateDecision.BLOCK


def test_check_blocks_network_disallowed():
    e = ContractEnforcer(NO_NETWORK)
    r = e.check("x", requires_network=True)
    assert r.decision == GateDecision.BLOCK
    assert r.violations[0].field == "allow_network"


def test_check_blocks_cost_over_cap():
    c = Contract(agent_id="cap", max_cost_per_action_cents=50)
    e = ContractEnforcer(c)
    r = e.check("x", estimated_cost_cents=51)
    assert r.decision == GateDecision.BLOCK
    assert r.violations[0].field == "max_cost_per_action_cents"


def test_check_blocks_deny_paths():
    e = ContractEnforcer(PATH_RESTRICTED)
    r = e.check("rf", paths_touched=["/etc/shadow"])
    assert r.decision == GateDecision.BLOCK
    assert r.violations[0].field == "deny_paths"


def test_check_blocks_path_not_in_allowlist():
    e = ContractEnforcer(PATH_RESTRICTED)
    r = e.check("rf", paths_touched=["/tmp/x"])
    assert r.decision == GateDecision.BLOCK
    assert r.violations[0].field == "allow_paths"


def test_check_clean_path_allowed():
    e = ContractEnforcer(PATH_RESTRICTED)
    r = e.check("rf", paths_touched=["/workspace/foo.py"])
    assert r.decision == GateDecision.ALLOW


def test_check_require_approval_decision():
    e = ContractEnforcer(APPROVAL_ONLY)
    r = e.check("shell")
    assert r.decision == GateDecision.REQUIRE_APPROVAL


def test_check_non_require_approval_tool_allows():
    e = ContractEnforcer(APPROVAL_ONLY)
    r = e.check("read_file")
    assert r.decision == GateDecision.ALLOW


def test_require_approval_skip_with_flag():
    e = ContractEnforcer(APPROVAL_ONLY)
    r = e.check("shell", require_approval=False)
    assert r.decision == GateDecision.ALLOW


def test_check_extra_metadata_merged():
    e = ContractEnforcer(OPEN)
    r = e.check("shell", extra={"task_id": "t1"})
    assert r.metadata["task_id"] == "t1"


def test_check_multiple_violations():
    c = Contract(
        agent_id="x",
        allow_tools=["rf"],
        allow_network=False,
        max_cost_per_action_cents=10,
    )
    e = ContractEnforcer(c)
    r = e.check("http_get", requires_network=True, estimated_cost_cents=999)
    assert r.decision == GateDecision.BLOCK
    fields = {v.field for v in r.violations}
    assert {"allow_tools", "allow_network", "max_cost_per_action_cents"} <= fields


# ── enforce() ───────────────────────────────────────────────────────────────

def test_enforce_returns_on_allow():
    e = ContractEnforcer(OPEN)
    r = e.enforce("shell")
    assert r.decision == GateDecision.ALLOW


def test_enforce_raises_on_block():
    e = ContractEnforcer(DENY_SHELL)
    with pytest.raises(ContractViolation) as exc_info:
        e.enforce("shell")
    assert exc_info.value.decision == GateDecision.BLOCK
    assert exc_info.value.result.decision == GateDecision.BLOCK


def test_enforce_raises_on_require_approval():
    e = ContractEnforcer(APPROVAL_ONLY)
    with pytest.raises(ContractViolation) as exc_info:
        e.enforce("shell")
    assert exc_info.value.decision == GateDecision.REQUIRE_APPROVAL


def test_enforce_appends_audit_on_block():
    e = ContractEnforcer(DENY_SHELL)
    try:
        e.enforce("shell")
    except ContractViolation:
        pass
    assert len(e.tail_audit()) >= 1
    entry = e.tail_audit(1)[0]
    assert entry["decision"] == GateDecision.BLOCK


def test_enforce_does_not_append_audit_when_flag_off():
    e = ContractEnforcer(DENY_SHELL)
    try:
        e.enforce("shell", append_audit=False)
    except ContractViolation:
        pass
    assert len(e.audit_trail._records) == 0


def test_enforce_with_context_object():
    ctx = ActionContext(tool_name="shell", arguments={}, estimated_cost_cents=0)
    e = ContractEnforcer(DENY_SHELL)
    with pytest.raises(ContractViolation):
        e.enforce(context=ctx)


def test_contract_violation_message():
    e = ContractEnforcer(DENY_SHELL)
    try:
        e.enforce("shell")
    except ContractViolation as exc:
        assert "Contract" in str(exc)
        assert "decision" in str(exc)
        assert exc.result is not None


def test_contract_violation_decision_property():
    viol = ContractViolation(
        GateResult(decision=GateDecision.BLOCK, violations=[
            GateViolation(field="deny_tools", message="Tool shell is blocked")
        ])
    )
    assert viol.decision == GateDecision.BLOCK


# ── Audit trail ──────────────────────────────────────────────────────────────

def test_audit_trail_appends_on_non_allow():
    e = ContractEnforcer(DENY_SHELL)
    r = e.check("shell")
    assert r.decision == GateDecision.BLOCK
    tail = e.tail_audit()
    assert len(tail) == 1
    assert tail[0]["tool_name"] == "shell"
    assert tail[0]["decision"] == GateDecision.BLOCK


def test_audit_trail_cleared_on_hot_swap():
    e = ContractEnforcer(DENY_SHELL)
    e.check("shell")
    assert len(e.tail_audit()) == 1
    e.replace_contract(OPEN)
    assert len(e.tail_audit()) == 0


def test_audit_capacity_limit():
    e = ContractEnforcer(DENY_SHELL, audit_capacity=3)
    for _ in range(5):
        e.check("shell")
    assert len(e.audit_trail._records) == 3


# ── Hermes MCP adapter ───────────────────────────────────────────────────────

class FakeContext:
    """Minimal stand-in for :class:`~agentcontract.sdk.HermesMCPContext`."""

    def __init__(self, tool_name: str = "shell") -> None:
        self.context = ActionContext(
            tool_name=tool_name,
            arguments={},
            estimated_cost_cents=0,
            paths_touched=[],
            requires_network=False,
        )


def test_hermes_context_allows_when_allowed():
    e = ContractEnforcer(OPEN)
    ctx = FakeContext("shell")
    result = e.check_hermes(HermesMCPContext(context=ctx.context))
    assert result.decision == GateDecision.ALLOW


def test_hermes_context_blocked_by_deny_tools():
    e = ContractEnforcer(DENY_SHELL)
    ctx = FakeContext("shell")
    result = e.check_hermes(HermesMCPContext(context=ctx.context))
    assert result.decision == GateDecision.BLOCK


def test_mcp_rejection_record_has_decision():
    r = GateResult(
        decision=GateDecision.BLOCK,
        violations=[GateViolation(field="deny_tools", message="blocked")],
    )
    rec = ContractEnforcer(OPEN).mcp_rejection_record(r)
    assert rec["contract_id"] == "open"
    assert rec["decision"] == "block"
    assert rec["violations"]


# ── Source label ─────────────────────────────────────────────────────────────

def test_repr_contains_contract_agent_id():
    e = ContractEnforcer(OPEN)
    assert "open" in repr(e)
