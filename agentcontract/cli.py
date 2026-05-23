#!/usr/bin/env python3
"""
agentcontract.cli
─────────────────
`agc` — command-line interface for agentcontract.

Commands
--------
agc init     Create starter contract in CWD or Hermes skill dir
agc validate  Validate a contract file and print result
agc gate      Dry-run a tool-call against a contract
agc audit     Print / tail audit log
agc schema    Emit JSON Schema to stdout
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.json import JSON
from rich.table import Table

from .core import (
    ActionContext,
    Contract,
    GateDecision,
    GateResult,
    GateViolation,
    contract_to_json_schema,
    gate,
)

console = Console()

CONTRACT_DIR_DEFAULT = Path.home() / ".hermes" / "contracts"
EXAMPLE_CONTRACT = """{
  "version": "1.0",
  "agent_id": "my-agent",
  "description": "Default development contract — permissive until tightened",
  "allow_tools": null,
  "deny_tools": [],
  "allow_paths": null,
  "deny_paths": [],
  "allow_network": true,
  "max_cost_per_action_cents": null,
  "require_approval": [],
  "max_iterations": null
}"""


# ── CLI group ──────────────────────────────────────────────────────────────


@click.group(
    "agc",
    invoke_without_command=True,
    context_settings={"auto_envvar_prefix": "AGC"},
)
@click.version_option(package_name="agentcontract")
@click.pass_context
def app(ctx: click.Context) -> None:
    """Agent Contract — JSON-native capability contracts for AI agents.

    Run  agc COMMAND --help  for per-command help.
    """
    if ctx.invoked_subcommand is None:
        click.echo(app.get_help(ctx))


# ── agc init ───────────────────────────────────────────────────────────────


@app.command("init")
@click.argument("name", default="default-contract")
@click.option(
    "--hermes",
    is_flag=True,
    default=False,
    help="Write into ~/.hermes/contracts/ instead of CWD",
)
def init_cmd(name: str, hermes: bool) -> None:
    """Write a starter contract file and print the resulting path.

    NAME is used as the filename stem (default: default-contract).
    """
    base = CONTRACT_DIR_DEFAULT if hermes else Path.cwd()
    base.mkdir(parents=True, exist_ok=True)
    dest = base / f"{name}.json"

    if dest.exists():
        console.print(f"[yellow]Already exists, skipping: {dest}[/yellow]")
        console.print(JSON(Path(dest).read_text()))
        return

    dest.write_text(EXAMPLE_CONTRACT + "\n")
    console.print(f"[green]✓ Contract written:[/green] {dest}")
    console.print("\nNext steps:")
    console.print(f"  [cyan]agc validate {dest}[/cyan]")
    console.print(f"  agc gate  {dest}  --action '{{{{'tool_name':'shell'}}}}'")


# ── agc validate ────────────────────────────────────────────────────────────


@app.command("validate")
@click.argument("contract_path", type=click.Path(exists=True, dir_okay=False))
def validate_cmd(contract_path: str) -> None:
    """Validate a contract file (schema + Pydantic checks)."""
    raw = Path(contract_path).read_text()
    try:
        contract = Contract.model_validate_json(raw)
    except Exception as exc:
        console.print(f"[red]✗ Validation failed:[/red] {exc}")
        sys.exit(1)

    console.print(f"[green]✓ Valid[/green] — contract '{contract.agent_id}' (v{contract.version})")
    console.print(f"  tools allowed:  {contract.allow_tools or '*'}")
    console.print(f"  tools denied:   {contract.deny_tools or 'none'}")
    console.print(f"  network:        {contract.allow_network}")
    console.print(f"  require_approval: {contract.require_approval or 'none'}")


# ── agc gate ────────────────────────────────────────────────────────────────


@app.command("gate")
@click.argument("contract_path", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--action",
    required=True,
    help='JSON string describing the action, e.g. \'{"tool_name":"shell","arguments":{...}}\'',
)
@click.option("--cost", type=int, default=0, help="Estimated cost in cents (default 0).")
@click.option(
    "--no-require-approval",
    is_flag=True,
    default=False,
    help="Skip the require_approval check ( approvals handled externally).",
)
def gate_cmd(
    contract_path: str,
    action: str,
    cost: int,
    no_require_approval: bool,
) -> None:
    """Dry-run a tool-call against a contract and print the decision."""
    raw = Path(contract_path).read_text()
    try:
        contract = Contract.model_validate_json(raw)
    except Exception as exc:
        console.print(f"[red]✗ Cannot parse contract:[/red] {exc}")
        sys.exit(1)

    try:
        action_dict = json.loads(action)
    except json.JSONDecodeError as exc:
        console.print(f"[red]✗ --action must be valid JSON:[/red] {exc}")
        sys.exit(1)

    ctx = ActionContext(
        tool_name=action_dict.get("tool_name", ""),
        arguments=action_dict.get("arguments", action_dict),
        estimated_cost_cents=cost,
        paths_touched=action_dict.get("paths_touched", []),
        requires_network=action_dict.get("requires_network", False),
    )

    result = gate(
        contract,
        ctx,
        require_approval=(not no_require_approval),
    )

    _print_result(result)


# ── agc audit ────────────────────────────────────────────────────────────────


@app.command("audit")
@click.option(
    "--contract",
    "contract_id",
    help="Filter by agent_id",
)
@click.option("--limit", type=int, default=20, help="Lines to show")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
def audit_cmd(contract_id: str | None, limit: int, as_json: bool) -> None:
    """Display recent audit entries (JSONL from ~/.hermes/contracts/audit.jsonl)."""
    audit_path = CONTRACT_DIR_DEFAULT / "audit.jsonl"
    if not audit_path.exists():
        console.print("[yellow]No audit log yet.[/yellow]")
        return

    lines = audit_path.read_text().splitlines()
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if contract_id and entry.get("contract_id") != contract_id:
            continue
        entries.append(entry)

    entries = entries[-limit:]
    if as_json:
        click.echo(json.dumps(entries, indent=2))
    else:
        if not entries:
            console.print("[yellow]No matching entries.[/yellow]")
            return
        table = Table(title=f"Audit log ({len(entries)} entries)")
        table.add_column("time", style="cyan", no_wrap=True)
        table.add_column("contract", style="magenta")
        table.add_column("tool", style="green")
        table.add_column("decision", style="yellow")
        table.add_column("violations", style="red")
        for e in entries:
            vlist = e.get("violations", [])
            v_str = ", ".join(v.get("field", "?") for v in vlist) if vlist else "-"
            dec = e.get("decision", "?")
            dec_style = {"allow": "green", "block": "red", "require_approval": "yellow"}.get(
                dec, "white"
            )
            table.add_row(
                e.get("timestamp", "?")[:19],
                e.get("contract_id", "?"),
                e.get("tool_name", "?"),
                f"[{dec_style}]{dec}[/{dec_style}]",
                v_str,
            )
        console.print(table)


# ── agc schema ───────────────────────────────────────────────────────────────


@app.command("schema")
def schema_cmd() -> None:
    """Emit the Contract JSON Schema (draft 2020-12) to stdout."""
    schema = contract_to_json_schema()
    console.print(JSON(json.dumps(schema, indent=2)))
    console.print(f"\n[dim]$id: {schema['$id']}[/dim]")


# ── Output helper ───────────────────────────────────────────────────────────


def _print_result(result: GateResult) -> None:
    tag = {
        GateDecision.ALLOW: "[green]✓ ALLOW[/green]",
        GateDecision.BLOCK: "[red]✗ BLOCK[/red]",
        GateDecision.REQUIRE_APPROVAL: "[yellow]⏸ REQUIRE_APPROVAL[/yellow]",
    }[result.decision]

    console.print(f"{tag}  ({result.metadata.get('contract', '?')})")

    if result.violations:
        table = Table(title="Violations")
        table.add_column("field", style="red")
        table.add_column("message")
        for v in result.violations:
            table.add_row(v.field, v.message)
        console.print(table)
    sys.exit(1 if result.decision == GateDecision.BLOCK else 0)
