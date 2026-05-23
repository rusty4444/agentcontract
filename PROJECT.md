# agentcontract — Plan

> **The gap:** Existing agent governance tools (cordum, SuperAgentX, SomaOS Gateway) are full
> control planes, framework-bound runtimes, or browser-only sandboxes. There is no **lightweight,
> JSON-native, framework-agnostic contract specification** that a security team can write once,
> version-control, and enforce as a pre-execution gate for *any* agent runtime (Hermes, LangChain,
> raw OpenAI function-calling, etc.).
>
> **The concept:** Declarative = JSON contract. What the agent *can* and *cannot* do:
> allowed tools, allowed directories, network access, cost cap, required_approval list. A
> single binary/call the developer drops into their tool loop; if the contract permits, the action
> runs; otherwise it's blocked and logged.
>
> **Contrast:** cordum is a your full rebuild; jailoc is only sandboxing; OPA/Violations is
> about generic policy — *agentcontract* = the missing contract-as-code + execution gate for the
> AI → Tool Call chain specifically.
>
> **Foundation:** Started 2026-05-23, Rusty4444, MIT.

---

## Architecture

```
agentcontract/
├── agentcontract/
│   ├── __init__.py          # Version, export
│   ├── schema.py            # Pydantic v2 Contract model
│   ├── engine.py            # gate(action) → allow | block | require_approval
│   ├── validator.py         # JSON Schema validate, schema drift check
│   ├── registry.py          # Load / list contracts by UUID or alias
│   ├── audit.py             # Append audit log line, read audit
│   ├── sdk.py               # HermesMCPFormatter, CallContext
│   └── formatters/          # Hermes, LangChain, generic JSON
├── cli.py                   # agc CLI (click)
├── pyproject.toml           # Poetry/PEP 621
├── README.md
└── contracts/
    └── example/
        └── default.json     # Starter template
```

---

## Roadmap

### MVP — Phase 1 (this session)
1. Repo created, private, v0.1.0 tag
2. `pyproject.toml` — project metadata, deps only on pydantic + click
3. `Contract` Pydantic v2 model with fields:
   - `version` (semver)
   - `agent_id`, `description`
   - `allow_tools` (None = all, list[string] = only this set)
   - `deny_tools` (always block, even if in allow_tools)
   - `allow_paths` / `deny_paths` (filesystem)
   - `allow_network` (bool)
   - `max_cost_per_action_cents` (cost guard)
   - `require_approval` (list[str] — tool names needing human sign-off)
   - `max_iterations` (cap recursion)
4. JSON Schema auto-generated from Pydantic model
5. `ValidationResult` — pass / fail with violations list
6. `engine.gate(context) → GateResult`
7. CLI:
   - `agc init [name]`
   - `agc validate <path>`
   - `agc gate <path> --action <json>`
8. Basic CLI smoke test passes
9. Code review with manus-agentic

### Phase 2 — next session
10. Formatters: `HermesMCPFormatter(Context)` → tool call string
11. Audit log (append JSONL)
12. `agc init` writes into Hermes skill directory pattern

---

## Design Decisions

| Decision | Choice | Why |
|---|---|---|
| Python | Yes | Hermes + AI ecosystem is Python-first |
| No OPA dependency | Ship JSON Schema + built-in | Zero extra deps, CI-friendly |
| JSON Schema | v2020-12, auto-generated | Human-readable, GitHub renders it |
| CLI entry | `agc` | Short, distinguishable from `ag` (Augmented Generation) |
| Provider to write contracts | JSON | AI agents can generate it natively |
| Registry key | UUID + alias | UUID = immutable identity; alias for workflow readability |
| Enforcers | Contextual gate + JSON-Schema check | Written + struct output |
| Generative tests | Manual smokes only | Keep tight, no golden master rot |
| License | MIT | Maximum adoption |

---

## Implementation Order

**Step 1** — Repo + pyproject + src layout
**Step 2** — Pydantic Contract model
**Step 3** — JSON Schema generation + validator
**Step 4** — Engine gate() function
**Step 5** — CLI with 3 commands
**Step 6** — Example contract + README
**Step 7** — Code review
