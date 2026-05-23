"""
agentcontract.core
─────────────────
Core schema and execution-gate engine.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ── Enums ──────────────────────────────────────────────────────────────────


class GateDecision(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    REQUIRE_APPROVAL = "require_approval"


class ContractFormat(str, Enum):
    JSON = "json"
    YAML = "yaml"


# ── Models ──────────────────────────────────────────────────────────────────


class Contract(BaseModel):
    """Declarative capability contract for an AI agent.

    Load from JSON/YAML, validate, and pass to :func:`engine.gate` before every
    tool call.

    Example
    -------
    >>> import json, pathlib
    >>> c = Contract.model_validate_json(pathlib.Path("contracts/default.json").read_text())
    >>> assert c.version == "1.0"
    """

    # Identity
    version: str = Field(default="1.0", pattern=r"^\d+\.\d+$")
    agent_id: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)

    # Tool allow / deny lists
    # None  → no restriction (all tools allowed)
    # list  → only these tools allowed (explicit allowlist)
    allow_tools: list[str] | None = None
    # Always blocked even if in allow_tools
    deny_tools: list[str] = Field(default_factory=list)

    # Filesystem
    allow_paths: list[str] | None = None   # glob allowed; None = all
    deny_paths: list[str] = Field(default_factory=list)  # always denied

    # Network
    allow_network: bool = True

    # Cost guard  (None = uncapped)
    max_cost_per_action_cents: int | None = Field(default=None, ge=0)

    # Approval gate  (tool names that require human sign-off)
    require_approval: list[str] = Field(default_factory=list)

    # Recursion / loop cap
    max_iterations: int | None = Field(default=None, ge=1)

    # ── Cross-field validation ─────────────────────────────────────────────

    @model_validator(mode="after")
    def check_conflicts(self) -> "Contract":
        allow = set(self.allow_tools) if self.allow_tools else set()
        deny = set(self.deny_tools)
        conflict = allow & deny
        if conflict:
            raise ValueError(
                f"Tools appear in both allow_tools and deny_tools: "
                f"{sorted(conflict)}"
            )
        return self

    @field_validator("allow_paths", "deny_paths")
    @classmethod
    def non_empty_strings(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        for item in v:
            if not item or not item.strip():
                raise ValueError("Path entries must be non-empty strings")
        return v


# ── Context & Result ────────────────────────────────────────────────────────


class ActionContext(BaseModel):
    """Describes the agent action about to be executed."""

    tool_name: str = Field(..., min_length=1)
    # Arguments as a JSON-serialisable mapping
    arguments: dict[str, Any] = Field(default_factory=dict)
    # Estimated cost in cents for this call (0 = free / local)
    estimated_cost_cents: int = Field(default=0, ge=0)
    # Filesystem paths this action touches
    paths_touched: list[str] = Field(default_factory=list)
    # Whether this action performs any network I/O
    requires_network: bool = False


class GateViolation(BaseModel):
    field: str
    message: str
    detail: dict[str, Any] | None = None


class GateResult(BaseModel):
    decision: GateDecision
    violations: list[GateViolation] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ── Engine ──────────────────────────────────────────────────────────────────


class GateError(Exception):
    """Raised by :func:`gate` when duplicate or malformed contract is detected."""

    def __init__(self, contract: Contract, violations: list[GateViolation]):
        self.contract = contract
        self.violations = violations
        messages = "; ".join(v.message for v in violations)
        super().__init__(f"Contract '{contract.agent_id}': {messages}")


def gate(
    contract: Contract,
    context: ActionContext,
    require_approval: bool = True,
) -> GateResult:
    """Evaluate whether *context* is permitted by *contract*.

    Returns a :class:`GateResult` with a single authoritative decision:

    1. ``require_approval=True`` in the contract, tool matches → ``require_approval``
    2. ``deny_tools`` matches → ``block``
    3. ``allow_tools`` is set and tool **not** in it → ``block``
    4. Any touched path matches ``deny_paths`` → ``block``
    5. Network required but ``allow_network=False`` → ``block``
    6. Cost exceeds cap → ``block``
    7. Otherwise → ``allow``

    Raises :class:`GateError` if the pre-condition checks fail
    (missing tool name, malformed paths).

    Parameters
    ----------
    contract:
        Loaded and validated :class:`Contract` instance.
    context:
        :class:`ActionContext` describing the proposed action.
    require_approval:
        When ``False``, skip the ``require_approval`` tool-name check
        (useful if an outer loop handles approvals separately).

    Returns
    -------
    GateResult
    """
    violations: list[GateViolation] = []

    # ── Tool allow / deny ─────────────────────────────────────────────────
    if contract.allow_tools is not None:
        if context.tool_name not in contract.allow_tools:
            violations.append(
                GateViolation(
                    field="allow_tools",
                    message=f"Tool '{context.tool_name}' not in allowlist: {contract.allow_tools}",
                )
            )

    if context.tool_name in contract.deny_tools:
        violations.append(
            GateViolation(
                field="deny_tools",
                message=f"Tool '{context.tool_name}' is explicitly denied by contract",
            )
        )

    # ── Filesystem ────────────────────────────────────────────────────────
    for path in context.paths_touched:
        # deny_paths always wins
        for pattern in contract.deny_paths:
            if _path_matches(path, pattern):
                violations.append(
                    GateViolation(
                        field="deny_paths",
                        message=f"Path '{path}' matches denied pattern '{pattern}'",
                        detail={"path": path, "pattern": pattern},
                    )
                )
        # allow_paths restricts
        if contract.allow_paths is not None:
            allowed = any(_path_matches(path, p) for p in contract.allow_paths)
            if not allowed:
                violations.append(
                    GateViolation(
                        field="allow_paths",
                        message=f"Path '{path}' not covered by allow patterns: {contract.allow_paths}",
                        detail={"path": path},
                    )
                )

    # ── Network ───────────────────────────────────────────────────────────
    if context.requires_network and not contract.allow_network:
        violations.append(
            GateViolation(
                field="allow_network",
                message="Action requires network but contract sets allow_network=false",
            )
        )

    # ── Cost cap ──────────────────────────────────────────────────────────
    if (
        contract.max_cost_per_action_cents is not None
        and context.estimated_cost_cents > contract.max_cost_per_action_cents
    ):
        violations.append(
            GateViolation(
                field="max_cost_per_action_cents",
                message=(
                    f"Estimated cost {context.estimated_cost_cents}¢ exceeds "
                    f"cap {contract.max_cost_per_action_cents}¢"
                ),
                detail={
                    "estimated": context.estimated_cost_cents,
                    "cap": contract.max_cost_per_action_cents,
                },
            )
        )

    # ── Approval gate ─────────────────────────────────────────────────────
    # Only fire require_approval when the tool passed all hard-denies;
    # a tool that is in deny_tools or not in allow_tools is a hard BLOCK
    # regardless of any require_approval listing.
    approval_violations_present = any(
        v.field in ("deny_tools", "allow_tools") for v in violations
    )
    if (
        require_approval
        and not approval_violations_present
        and context.tool_name in contract.require_approval
        and not any(v.field == "require_approval" for v in violations)
    ):
        return GateResult(
            decision=GateDecision.REQUIRE_APPROVAL,
            metadata={"tool": context.tool_name, "contract": contract.agent_id},
        )

    # ── Final decision ────────────────────────────────────────────────────
    if violations:
        return GateResult(
            decision=GateDecision.BLOCK,
            violations=violations,
            metadata={"contract": contract.agent_id},
        )

    return GateResult(
        decision=GateDecision.ALLOW,
        metadata={"contract": contract.agent_id},
    )


# ── Helpers ─────────────────────────────────────────────────────────────────


def _path_matches(path: str, pattern: str) -> bool:
    """Return True if *path* matches *pattern*, which can be a glob string."""
    # Exact match
    if path == pattern:
        return True
    # Prefix match for directory patterns (e.g. "/tmp/")
    if pattern.endswith("/"):
        return path.startswith(pattern)
    # Glob
    import fnmatch
    return fnmatch.fnmatch(path, pattern)


def contract_to_json_schema() -> dict[str, Any]:
    """Return JSON Schema (draft 2020-12) for :class:`Contract`."""
    # Build from the pydantic-core schema; keep it human-readable
    schema: dict[str, Any] = Contract.model_json_schema(
        ref_template="#/definitions/{model}"
    )
    # Strip internal Pydantic annotations for the published schema
    schema.pop("$defs", None)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://github.com/rusty4444/agentcontract/schema/v1",
        **schema,
    }
