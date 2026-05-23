# agentcontract
# Copyright (c) 2026 Rusty4444
# SPDX-License-Identifier: MIT

"""agentcontract — JSON-native, framework-agnostic governance layer for LLM agents.

Import the public surface from here::

    from agentcontract import Contract, ActionContext, GateDecision, gate
"""

from agentcontract.core import (
    ActionContext,
    Contract,
    GateDecision,
    GateError,
    GateResult,
    GateViolation,
    contract_to_json_schema,
    gate,
)
from agentcontract.sdk import HermesMCPContext, HermesMCPFormatter

__all__ = [
    "Contract",
    "ActionContext",
    "GateDecision",
    "GateError",
    "GateResult",
    "GateViolation",
    "HermesMCPContext",
    "HermesMCPFormatter",
    "contract_to_json_schema",
    "gate",
]
__version__ = "0.1.0"
