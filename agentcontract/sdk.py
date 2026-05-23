"""
agentcontract.sdk
─────────────────
Formatter helpers that make it easy to integrate agentcontract with
Hermes MCP tool-call logging and other LLM runtimes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from agentcontract.core import (
    ActionContext,
    Contract,
    GateDecision,
    GateResult,
    gate,
)


# ── Hermes-specific context ───────────────────────────────────────────────────


class HermesMCPContext(BaseModel):
    """Minimal context model shaped like a Hermes tool invocation.

    Wraps an :class:`~agentcontract.core.ActionContext` with the fields the
    Hermes gateway has available at tool-call time.

    Fields
    ------
    context:
        The generic :class:`~agentcontract.core.ActionContext` describing
        the action being gated.
    task_id:
        The Hermes ``task_id`` for this run (from
        :meth:`AIAgent.run_conversation`).
    session_id:
        The active session UUID.
    agent_id:
        Logical agent name — may differ from the contract's ``agent_id``
        when several agents share one contract.
    raw_tool_call:
        The full ``ToolCall`` object as received from the Hermes gateway
        (serialised to dict for type flexibility).
    """

    context: ActionContext
    task_id: str | None = None
    session_id: str | None = None
    agent_id: str | None = None
    raw_tool_call: dict[str, Any] | None = None


class HermesMCPFormatter:
    """Convert a :class:`~agentcontract.core.GateResult` into an MCP-friendly payload.

    Designed for use in a Hermes pre-execution hook so the rejection is
    surfaced as a structured tool log entry, not a bare Python exception.

    Example
    -------
    >>> from agentcontract.sdk import HermesMCPFormatter, HermesMCPContext
    >>> from agentcontract.core import Contract, ActionContext, gate
    >>> import json, pathlib
    >>>
    >>> c = Contract.model_validate_json(
    ...     pathlib.Path("contracts/default.json").read_text()
    ... )
    >>> ctx = HermesMCPContext(
    ...     context=ActionContext(
    ...         tool_name="shell",
    ...         arguments={"cmd": "sudo rm -rf /"},
    ...     ),
    ...     task_id="abc-123",
    ...     session_id="sess-001",
    ... )
    >>> result = gate(c, ctx.context)
    >>> fmt = HermesMCPFormatter(c)
    >>> record = fmt.format_gate_rejection(result)
    >>> json.dumps(record, indent=2)  # doctest: +ELLIPSIS
    {...
    """

    def __init__(self, contract: Contract) -> None:
        self.contract = contract

    # ── MCP tool log record ────────────────────────────────────────────────

    def format_gate_rejection(self, result: GateResult) -> dict[str, Any]:
        """Format a rejection into the shape Hermes MCP call log expects."""
        return {
            "tool_call_id": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "contract_id": self.contract.agent_id,
            "contract_version": self.contract.version,
            "decision": result.decision.value,
            "approved_by": None,
            "violations": [v.model_dump() for v in result.violations],
            "appended_to_audit": False,
            "metadata": result.metadata,
        }

    # ── Human-readable summary ─────────────────────────────────────────────

    def format_rejection_summary(self, result: GateResult) -> str:
        """Return a plain-text rejection for displaying to a human."""
        lines = [
            f"\U0001f5e1  Contract '{self.contract.agent_id}' blocked the action.",
            f"    Decision: {result.decision.value}",
            "",
        ]
        for v in result.violations:
            lines.append(f"  \u25b8 [{v.field}] {v.message}")
        return "\n".join(lines)

    # ── Hermes vault credential context ────────────────────────────────────

    def format_vault_context(self, mcp_ctx: HermesMCPContext) -> dict[str, Any]:
        """Build the credential context dict for an MCP-attached Hermes run.

        Passes through without accessing secrets directly — credentials should
        be resolved via ``mcp_hermes_vault_get_ephemeral_env`` at call time.
        """
        raw = mcp_ctx.raw_tool_call or {}
        return {
            "task_id": mcp_ctx.task_id,
            "session_id": mcp_ctx.session_id,
            "agent_id": mcp_ctx.agent_id or raw.get("agent_id"),
            "contract_id": self.contract.agent_id,
            "contract_version": self.contract.version,
        }

    # ── Audit log append ───────────────────────────────────────────────────

    def append_audit(self, result: GateResult) -> dict[str, Any]:
        """Append the gate decision to the JSONL audit log.

        Logs to ``~/.hermes/contracts/audit.jsonl``.
        Returns the written record (with ``appended_to_audit: true`` set).
        """
        from pathlib import Path

        audit_dir = Path.home() / ".hermes" / "contracts"
        audit_dir.mkdir(parents=True, exist_ok=True)
        audit_path = audit_dir / "audit.jsonl"

        record: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "contract_id": self.contract.agent_id,
            "contract_version": self.contract.version,
            "decision": result.decision.value,
            "tool_name": result.metadata.get("tool", "?"),
            "violations": [v.model_dump() for v in result.violations],
            "metadata": result.metadata,
        }

        with audit_path.open("a") as fh:
            fh.write(json.dumps(record) + "\n")

        record["appended_to_audit"] = True
        return record


# ── LangChain tool wrapper ───────────────────────────────────────────────────


class LangChainGate:
    """Thin adapter showing how agentcontract can wrap a LangChain tool run.

    Use it as a decorator or call ``check()`` before
    ``tool._run(**kwargs)``.

    Example
    -------
    >>> gate_fn = LangChainGate(contract)
    >>> ok, result = gate_fn.check("shell", kwargs={"cmd": "ls"}, requires_network=False)
    >>> if not ok:
    ...     print(Formatter(contract).format_rejection_summary(result))
    """

    def __init__(self, contract: Contract) -> None:
        self.contract = contract

    def check(
        self,
        tool_name: str,
        kwargs: dict[str, Any] | None = None,
        cost_cents: int = 0,
        requires_network: bool = False,
    ) -> tuple[bool, GateResult]:
        """Run the gate.  Returns ``(True, result)`` on allow; blocks otherwise."""
        result = gate(
            self.contract,
            ActionContext(
                tool_name=tool_name,
                arguments=kwargs or {},
                estimated_cost_cents=cost_cents,
                requires_network=requires_network,
            ),
        )
        return result.decision == GateDecision.ALLOW, result


# ── Plain preflight ──────────────────────────────────────────────────────────


class Preflight:
    """Stateless one-liner for any runtime that needs a quick allow/block call.

    Usage
    -----
    >>> pf = Preflight(contract)
    >>> ok, result = pf("shell", requires_network=False)
    """

    def __init__(self, contract: Contract) -> None:
        self.contract = contract

    def __call__(
        self,
        tool_name: str,
        cost_cents: int = 0,
        requires_network: bool = False,
    ) -> tuple[bool, GateResult]:
        return LangChainGate(self.contract).check(
            tool_name=tool_name,
            cost_cents=cost_cents,
            requires_network=requires_network,
        )
