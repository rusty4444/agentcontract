"""
agentcontract.enforcement
────────────────────────
Framework-agnostic runtime enforcement layer.

Holds a :class:`~agentcontract.core.Contract` in process memory — not in the
LLM context window — and exposes a single callable gate interface that every
tool invocation must pass through *before* execution.

Import::from agentcontract.enforcement import (
    ContractEnforcer,
    ContractViolation,
)
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from agentcontract.core import (
    ActionContext,
    Contract,
    GateDecision,
    GateResult,
    GateViolation,
    gate,
)
from agentcontract.sdk import HermesMCPContext, HermesMCPFormatter


# ── File/text loaders ─────────────────────────────────────────────────────────


def _load_contract_from_text(raw: str, *, source: str | None = None) -> Contract:
    """Parse a JSON or YAML contract string."""
    try:
        return Contract.model_validate_json(raw)
    except Exception:
        try:
            import yaml  # type: ignore

            data = yaml.safe_load(raw)
            if data is None:
                raise ValueError("YAML content is empty")
            return Contract.model_validate(data)
        except ImportError:
            raise ValueError(
                "YAML contract strings require the 'yaml' package.  "
                "Install with: pip install pyyaml"
            ) from None


def _load_contract_from_path(path: str | Path) -> Contract:
    """Read and validate a contract file from a filesystem path."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in (".yml", ".yaml"):
        try:
            import yaml  # type: ignore

            data = yaml.safe_load(p.read_text())
            if data is None:
                raise ValueError("YAML content is empty")
            return Contract.model_validate(data)
        except ImportError:
            raise ValueError(
                "YAML contract files require the 'yaml' package.  "
                "Install with: pip install pyyaml"
            ) from None
    return Contract.model_validate_json(p.read_text())


# ── Exceptions ────────────────────────────────────────────────────────────────


class ContractViolation(Exception):
    """Raised by :meth:`ContractEnforcer.enforce` on BLOCK / REQUIRE_APPROVAL.

    Attributes
    ----------
    result:
        Full :class:`~agentcontract.core.GateResult` for structured inspection.
    decision:
        Shortcut to ``result.decision.value``.
    """

    def __init__(self, result: GateResult, *, decision: str | None = None) -> None:
        self.result: GateResult = result
        self._decision = decision or result.decision.value
        parts = [f"[{v.field}] {v.message}" for v in result.violations]
        msg_parts = "; ".join(parts) or "(no violations)"
        msg = (
            f"Contract '{result.metadata.get('contract', '?')}' "
            f"decision={self._decision}: " + msg_parts
        )
        super().__init__(msg)

    @property
    def decision(self) -> str:
        """``"block"`` or ``"require_approval"``."""
        return self._decision


# ── Thread-safe in-memory audit ring ─────────────────────────────────────────


class AuditTrail:
    """Ring buffer of recent non-ALLOW gate decisions."""

    MAX_DEFAULT = 256

    def __init__(self, maxlen: int = MAX_DEFAULT) -> None:
        self._records: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._maxlen = maxlen

    def append(self, result: GateResult, ctx: ActionContext) -> None:
        with self._lock:
            self._records.append(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "contract_id": result.metadata.get("contract", "?"),
                    "tool_name": ctx.tool_name,
                    "decision": result.decision.value,
                    "violation_fields": [v.field for v in result.violations],
                }
            )
            while len(self._records) > self._maxlen:
                self._records.pop(0)

    def tail(self, n: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._records[-n:])

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


# ── Main enforcer ─────────────────────────────────────────────────────────────


class ContractEnforcer:
    """Hold a parsed contract in process memory and gate every tool call against it.

    The contract lives in interpreter state — not in the LLM context window — so
    it cannot be accidentally evicted by a long-running agent loop.

    Parameters
    ----------
    contract:
        * String path to a JSON or YAML contract file.
        * Raw JSON / YAML string content of the contract.
        * An already-parsed :class:`~agentcontract.core.Contract`.
    source:
        Provenance label stored in audit records.
        Defaults to the path, ``"<string>"``, or ``"<Contract …>"``.
    audit_capacity:
        Max entries to keep in the in-memory ring buffer (default 256).

    Examples
    --------
    .. code-block:: python

        from agentcontract import ContractEnforcer

        enforcer = ContractEnforcer("contracts/default.json")
        ok, result = enforcer.check("shell", estimated_cost_cents=0)
        if not ok:
            return "Blocked:" + ", ".join(v.message for v in result.violations)

    .. code-block:: python

        try:
            enforcer.enforce("execute_code", arguments={"code": "x = 1"})
        except ContractViolation as exc:
            return str(exc)
    """

    def __init__(
        self,
        contract: str | Path | Contract,
        *,
        source: str | None = None,
        audit_capacity: int = 256,
    ) -> None:
        if isinstance(contract, Contract):
            self._contract = contract
            self._source = source or f"<Contract {contract.agent_id}>"
        elif isinstance(contract, Path):
            self._contract = _load_contract_from_path(contract)
            self._source = source or str(contract)
        elif "\n" in contract or contract.strip().startswith("{"):
            self._contract = _load_contract_from_text(contract, source=source)
            self._source = source or "<raw-string>"
        else:
            self._contract = _load_contract_from_path(contract)
            self._source = source or str(contract)

        self._lock = threading.Lock()
        self.audit_trail = AuditTrail(maxlen=audit_capacity)
        self._mcp_formatter = HermesMCPFormatter(self._contract)

    # ── Properties ──────────────────────────────────────────────────────────

    @property
    def contract(self) -> Contract:
        """Currently held :class:`~agentcontract.core.Contract`."""
        return self._contract

    @property
    def source(self) -> str:
        """Label for where this contract was loaded from."""
        return self._source

    def __repr__(self) -> str:
        c = self._contract
        return (
            f"ContractEnforcer(agent={c.agent_id!r}, v={c.version!r}, "
            f"deny_tools={c.deny_tools!r}, allow_tools={c.allow_tools!r})"
        )

    # ── Hot-swap ─────────────────────────────────────────────────────────────

    def replace_contract(
        self,
        contract: str | Path | Contract,
        *,
        source: str | None = None,
    ) -> None:
        """Atomically swap the active contract in-process.

        Subsequent gate calls use the new contract.  Bad contracts are caught,
        logged to stderr, and do **not** replace the current contract.
        """
        try:
            if isinstance(contract, Contract):
                new, new_src = contract, source or repr(contract)
            elif isinstance(contract, Path):
                new, new_src = _load_contract_from_path(contract), source or str(contract)
            elif "\n" in contract or contract.strip().startswith("{"):
                new, new_src = _load_contract_from_text(contract, source=source), source or "<raw-string>"
            else:
                new, new_src = _load_contract_from_path(contract), source or str(contract)
        except Exception as exc:
            print(f"[agentcontract] contract hot-swap rejected: {exc}", flush=True)
            return
        with self._lock:
            self._contract = new
            self._source = new_src
            self._mcp_formatter = HermesMCPFormatter(self._contract)
            self.audit_trail.clear()

    # ── Return-value evaluation ──────────────────────────────────────────────

    def check(
        self,
        tool_name: str,
        *,
        arguments: dict[str, Any] | None = None,
        estimated_cost_cents: int = 0,
        paths_touched: list[str] | None = None,
        requires_network: bool = False,
        require_approval: bool = True,
        append_audit: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> GateResult:
        """Evaluate one tool call against the held contract without raising.

        Parameters
        ----------
        tool_name:
            Name of the tool being called.
        arguments:
            Tool keyword arguments — stored for audit metadata only.
        estimated_cost_cents:
            Per-action cost in USD cents.  ``0`` = local / free.
        paths_touched:
            Filesystem paths this action reads or writes.
        requires_network:
            Whether the action performs outbound network I/O.
        require_approval:
            Honor ``require_approval`` list in the contract.  ``False`` skips it.
        append_audit:
            Write non-ALLOW decisions to the in-memory ring buffer.
        extra:
            Additional ``metadata`` merged into the returned result.

        Returns
        -------
        GateResult
        """
        ctx = ActionContext(
            tool_name=tool_name,
            arguments=arguments or {},
            estimated_cost_cents=estimated_cost_cents,
            paths_touched=paths_touched or [],
            requires_network=requires_network,
        )
        with self._lock:
            result = gate(self._contract, ctx, require_approval=require_approval)
            if append_audit and result.decision != GateDecision.ALLOW:
                self.audit_trail.append(result, ctx)
            if extra:
                result.metadata.update(extra)
            return result

    # ── Exception-based evaluation ──────────────────────────────────────────

    def enforce(
        self,
        tool_name: str | None = None,
        *,
        arguments: dict[str, Any] | None = None,
        estimated_cost_cents: int = 0,
        paths_touched: list[str] | None = None,
        requires_network: bool = False,
        require_approval: bool = True,
        append_audit: bool = True,
        context: ActionContext | None = None,
    ) -> GateResult:
        """Evaluate and *raise* :class:`ContractViolation` on BLOCK / REQUIRE_APPROVAL.

        .. code-block:: python

            try:
                enforcer.enforce("shell", arguments={"cmd": "ls"})
            except ContractViolation as exc:
                return str(exc)

        One of ``tool_name`` (plus optional kwargs) or ``context`` must be provided.

        Parameters
        ----------
        tool_name:
            Name of the tool — required when ``context`` is not supplied.
        arguments:
            Keyword arguments passed through to the tool; stored for audit metadata.
        estimated_cost_cents:
            Per-action cost in USD cents.  ``0`` = local / free.
        paths_touched:
            Filesystem paths this action reads or writes.
        requires_network:
            Whether the action performs outbound network I/O.
        require_approval:
            Honor ``require_approval`` list in the contract.  ``False`` bypasses it.
        append_audit:
            Write non-ALLOW decisions to the in-memory ring buffer.
        context:
            Pre-built :class:`~agentcontract.core.ActionContext`.  When provided
            all per-argument kwargs are ignored — only ``require_approval`` and
            ``append_audit`` are still honoured.
        """
        if context is None and tool_name is None:
            raise ValueError(
                "enforce() requires either 'tool_name' or 'context'."
            )
        if context:
            ctx = context
        else:
            ctx = ActionContext(
                tool_name=tool_name,  # non-null asserted above
                arguments=arguments or {},
                estimated_cost_cents=estimated_cost_cents,
                paths_touched=paths_touched or [],
                requires_network=requires_network,
            )
        with self._lock:
            result = gate(self._contract, ctx, require_approval=require_approval)
            if result.decision != GateDecision.ALLOW:
                exc = ContractViolation(result)
                if append_audit:
                    self.audit_trail.append(result, ctx)
                raise exc
            # ALLOW — record so the ring shows recent clean passes
            self.audit_trail.append(result, ctx)
        return result

    # ── Hermes MCP adapter ───────────────────────────────────────────────────

    def check_hermes(self, mcp_ctx: HermesMCPContext) -> GateResult:
        """Evaluate a Hermes MCP pre-call context against the gate.

        Appends non-ALLOW decisions to both the in-memory ring buffer and the
        persistent JSONL log via :class:`~agentcontract.sdk.HermesMCPFormatter`.
        """
        ctx = mcp_ctx.context
        result = self.check(
            tool_name=ctx.tool_name,
            arguments=ctx.arguments,
            estimated_cost_cents=ctx.estimated_cost_cents,
            paths_touched=ctx.paths_touched,
            requires_network=ctx.requires_network,
        )
        if result.decision != GateDecision.ALLOW:
            self._mcp_formatter.append_audit(result)
        return result

    def mcp_rejection_record(self, result: GateResult) -> dict[str, Any]:
        """Return a structured rejection record for MCP tool-log injection."""
        return self._mcp_formatter.format_gate_rejection(result)

    # ── Context manager ──────────────────────────────────────────────────────

    @contextmanager
    def session(self,) -> Generator[ContractEnforcer, None, None]:
        """Yield self — annotated with context-manager life-cycle, no setup.

        The contract is already in process memory by construction; the ``with``
        block does no work on entry or exit.
        """
        try:
            yield self
        finally:
            pass

    # ── Audit inspect ────────────────────────────────────────────────────────

    def tail_audit(self, n: int = 10) -> list[dict[str, Any]]:
        """Return the last *n* audit entries from the in-memory ring buffer."""
        return self.audit_trail.tail(n)


__all__ = [
    "AuditTrail",
    "ContractEnforcer",
    "ContractViolation",
    "_load_contract_from_text",
    "_load_contract_from_path",
]
