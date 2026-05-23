"""
agentcontract.sdk
─────────────────
Formatter helpers that make it easy to integrate agentcontract with
Hermes MCP tool-call logging and other LLM runtimes.
"""

from __future__ import annotations

import json
import textwrap
from datetime import datetime, timezone
from typing import Any

from agentcontract.core import (
    ActionContext,
    Contract,
    GateDecision,
    GateResult,
    gate,
)


# ── Hermes MCP formatter ────────────────────────────────────────────────────


class HermesMCPFormatter:
    """Convert a :class:`GateResult` into an MCP-friendly payload.

    Designed for use in a Hermes pre-execution hook so the rejection is
    surfaced as a structured tool log entry, not a bare Python exception.

    Example
    -------
    >>> from agentcontract.sdk import HermesMCPFormatter
    >>> from agentcontract.core import Contract, ActionContext, gate
    >>> import json
    >>>
    >>> c = Contract.model_validate_json(Path("contracts/default.json").read_text())
    >>> ctx = ActionContext(tool_name="shell", arguments={"cmd": "sudo rm -rf /"})
    >>> result = gate(c, ctx)
    >>> fmt = HermesMCPFormatter(c)
    >>> record = fmt.format_gate_rejection(result)
    >>> json.dumps(record, indent=2)
    {

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
            f"⬛  Contract '{self.contract.agent_id}' blocked the action.",
            f"    Decision: {result.decision.value}",
            "",
        ]
        for v in result.violations:
            lines.append(f"  ▸ [{v.field}] {v.message}")
        return "\n".join(lines)

    # ── Hermes vault credential context (stub) ─────────────────────────────

    def append_audit(self, result: GateResult) -> dict[str, Any]:
        """Append the gate decision to the JSONL audit log.

        Logs to ``~/.hermes/contracts/audit.jsonl``.
        Returns the written record.
        """
        from pathlib import Path

        audit_dir = Path.home() / ".hermes" / "contracts"
        audit_dir.mkdir(parents=True, exist_ok=True)
        audit_path = audit_dir / "audit.jsonl"

        record = {
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
    ...     console.print(Formatter(contract).format_rejection_summary(result))
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
