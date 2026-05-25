# agentcontract
<p align="center">
  <a href="https://buymeacoffee.com/rusty4" target="_blank">
    <img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="50">
  </a>
</p>



**Agent contracts that don't lie.**

A JSON-native, framework-agnostic capability contract for LLM-powered agents. Declare
what an agent *can* and *cannot* do — which tools, which files, which network calls,
which cost caps — and get a single pre-execution gate that any runtime (Hermes, LangChain,
raw OpenAI function-calling) calls before every action.

```
agc init default-contract        # scaffold a contract
agc validate contracts/default.json
agc gate contracts/default.json --action '{"tool_name":"shell","estimated_cost_cents":0}'
agc gui                          # local browser GUI for setup + dry-runs
```

![agentcontract local setup GUI](docs/assets/agentcontract-gui.png)

Philosophy: contracts live in version control, can be audited in CI, and work over JavaScript and every LLM framework today.

---

## Gap

The agent-governance space has:

| Tool | Stars | What it is |
|---|---|---|
| [cordum](https://github.com/cordum-io/cordum) | ⭐480 | Go-based full control plane + MCP firewall |
| [SuperAgentX](https://github.com/superagentxai/superagentX) | ⭐200 | Python agent framework (opinionated) |
| [SomaOS Gateway](https://github.com/TryKosm/agentic-browser-ops-platform) | ⭐14 | API-key gated approval router |
| [jailoc](https://github.com/seznam/jailoc) | ⭐25 | Docker sandbox for OpenCode agents |
| **agentcontract** | — | **JSON contract + single pre-execution gate, zero framework lock-in** |

agentcontract is the smallest unit of governance: a JSON document validated at
compile-time, enforced at runtime, auditable without any server.

---

## Installation

```bash
pip install agentcontract
```

### Workflow basics

```bash
# Create a contract in your project or Hermes skill dir
agc init my-agent --hermes

# Open the local GUI to edit, validate, save, and dry-run gate decisions
agc gui

# Validate before committing
agc validate ~/.hermes/contracts/my-agent.json

# Test a tool call against the contract
agc gate ~/.hermes/contracts/my-agent.json \
    --action '{"tool_name":"shell","estimated_cost_cents":0}'
```

### Browser GUI

`agc gui` starts a localhost-only setup app at `http://127.0.0.1:8765/` with no
extra dependencies. Use it to:

- edit the contract JSON with a production-safe starter template;
- validate schema and cross-field rules before saving;
- dry-run an action payload and see `ALLOW`, `BLOCK`, or `REQUIRE_APPROVAL`;
- save the resulting contract to your project or `~/.hermes/contracts/` path.

---

## Contract schema

```json
{
  "version": "1.0",
  "agent_id": "my-agent",
  "description": "Development sandbox contract",
  "allow_tools": ["shell", "read_file", "write_file"],
  "deny_tools": ["execute_code"],
  "allow_paths": ["~/dev/"],
  "deny_paths": ["~/.ssh/", "~/.hermes/.env"],
  "allow_network": false,
  "max_cost_per_action_cents": 100,
  "require_approval": ["execute_code"],
  "max_iterations": 90
}
```

### Fields

| Field | Type | Default | Meaning |
|---|---|---|---|
| `version` | string | `"1.0"` | Schema version |
| `agent_id` | string | *required* | Stable identity for this contract |
| `description` | string | `""` | Human-readable purpose |
| `allow_tools` | `[string] \| null` | `null` | `null` = all tools allowed; list = explicit allowlist |
| `deny_tools` | `[string]` | `[]` | Always blocked, even if in `allow_tools` |
| `allow_paths` | `[string] \| null` | `null` | `null` = all paths; list = filesystem glob allowlist |
| `deny_paths` | `[string]` | `[]` | Always denied filesystem paths (globs supported) |
| `allow_network` | bool | `true` | Whether this agent may make outbound network calls |
| `max_cost_per_action_cents` | `int \| null` | `null` | Cap per-action cost in USD cents |
| `require_approval` | `[string]` | `[]` | Tool names that need human sign-off before execution |
| `max_iterations` | `int \| null` | `null` | Cap recursive loop depth |

---

## Engine (Python SDK)

```python
from agentcontract.core import Contract, ActionContext, GateDecision, gate

contract = Contract.model_validate_json(Path(".agentcontract.json").read_text())
result  = gate(
    contract,
    ActionContext(
        tool_name="shell",
        arguments={"command": "rm -rf /"},
        estimated_cost_cents=0,
        paths_touched=["/"],
        requires_network=False,
    ),
)

# result.decision == GateDecision.BLOCK
# result.violations → list[GateViolation]
```

---

## Hermes integration

Use the `HermesMCPFormatter` to produce MCP-ready context from a gate rejection:

```python
from agentcontract.sdk import HermesMCPFormatter

formatter = HermesMCPFormatter(contract)
mcp_msg   = formatter.format_gate_rejection(result)
# → uses mcp_hermes_vault_* get_credential_metadata tools under the hood
```

Register this as a pre-execution hook in `hermes_cli/commands.py`:

```python
# In your Hermes setup, before every tool call:
result = gate(active_contract, context)
if result.decision == GateDecision.BLOCK:
    ...reject + log...
elif result.decision == GateDecision.REQUIRE_APPROVAL:
    ...buffer for human...
```

---

## Audit log

Every gate decision is JSONL-appended to `~/.hermes/contracts/audit.jsonl`.
Tail it with:

```bash
agc audit
agc audit --contract my-agent --limit 50
```

---

## CI usage

```yaml
# .github/workflows/contract-check.yml
- name: Validate agent contracts
  run: |
    pip install agentcontract
    agc validate contracts/prod-agent.json
    agc gate contracts/prod-agent.json \
        --action '{"tool_name":"execute_code","arguments":{}}'
```

---

## License

MIT — see [LICENSE](LICENSE).
