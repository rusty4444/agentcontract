"""Local browser GUI for creating, validating, and trying agent contracts.

The GUI intentionally uses only the Python standard library so ``agentcontract``
stays lightweight: ``agc gui`` starts a localhost HTTP server and opens a single
HTML app that calls JSON endpoints for validation, scaffolding, and gate checks.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agentcontract.core import ActionContext, Contract, GateResult, gate

DEFAULT_GUI_CONTRACT = {
    "version": "1.0",
    "agent_id": "my-agent",
    "description": "Development sandbox contract",
    "allow_tools": ["shell", "read_file", "write_file"],
    "deny_tools": ["execute_code"],
    "allow_paths": ["./"],
    "deny_paths": ["~/.ssh/", "~/.hermes/.env"],
    "allow_network": False,
    "max_cost_per_action_cents": 100,
    "require_approval": ["shell"],
    "max_iterations": 90,
}

DEFAULT_GUI_ACTION = {
    "tool_name": "shell",
    "arguments": {"command": "ls"},
    "estimated_cost_cents": 0,
    "paths_touched": ["./"],
    "requires_network": False,
}


SAFE_SAVE_ROOTS = (Path.cwd().resolve(), Path.home().joinpath(".hermes", "contracts").resolve())


def _safe_contract_filename(agent_id: str) -> str:
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", agent_id).strip(".-")
    return f"{safe_stem or 'contract'}.json"


def _resolve_save_destination(destination: str | None, agent_id: str) -> Path:
    """Resolve a GUI save destination while preventing traversal writes."""
    raw_dest = Path(destination).expanduser() if destination else Path(_safe_contract_filename(agent_id))
    dest = raw_dest.resolve() if raw_dest.is_absolute() else (Path.cwd() / raw_dest).resolve()
    if not any(dest.is_relative_to(root) for root in SAFE_SAVE_ROOTS):
        roots = ", ".join(str(root) for root in SAFE_SAVE_ROOTS)
        raise ValueError(f"Save path must be inside one of: {roots}")
    return dest


def validate_contract_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate contract JSON from the GUI and return a JSON-safe result."""
    try:
        contract = Contract.model_validate(payload)
    except ValidationError as exc:
        return {"ok": False, "errors": exc.errors(include_url=False)}
    except Exception as exc:  # defensive: pydantic may wrap cross-field errors
        return {"ok": False, "errors": [{"loc": ["contract"], "msg": str(exc)}]}
    return {"ok": True, "contract": contract.model_dump()}


def gate_payload(contract_payload: dict[str, Any], action_payload: dict[str, Any]) -> dict[str, Any]:
    """Run the gate against GUI-provided contract and action payloads."""
    validation = validate_contract_payload(contract_payload)
    if not validation["ok"]:
        return {"ok": False, "stage": "contract", "errors": validation["errors"]}

    try:
        context = ActionContext(
            tool_name=action_payload.get("tool_name", ""),
            arguments=action_payload.get("arguments", {}),
            estimated_cost_cents=action_payload.get(
                "estimated_cost_cents", action_payload.get("cost", 0)
            ),
            paths_touched=action_payload.get("paths_touched", []),
            requires_network=action_payload.get("requires_network", False),
        )
    except ValidationError as exc:
        return {"ok": False, "stage": "action", "errors": exc.errors(include_url=False)}

    result: GateResult = gate(Contract.model_validate(validation["contract"]), context)
    return {"ok": True, "result": result.model_dump(mode="json")}


def save_contract_payload(payload: dict[str, Any], destination: str | None = None) -> dict[str, Any]:
    """Validate and save a contract payload to disk."""
    validation = validate_contract_payload(payload)
    if not validation["ok"]:
        return {"ok": False, "errors": validation["errors"]}

    agent_id = validation["contract"]["agent_id"]
    try:
        dest = _resolve_save_destination(destination, agent_id)
    except ValueError as exc:
        return {"ok": False, "errors": [{"loc": ["path"], "msg": str(exc)}]}
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(validation["contract"], indent=2) + "\n")
    return {"ok": True, "path": str(dest), "contract": validation["contract"]}


class AgentContractGUIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the local GUI app."""

    server_version = "agentcontract-gui/0.0.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        if self.path in ("/", "/index.html"):
            self._send_html(_index_html())
            return
        if self.path == "/api/defaults":
            self._send_json(
                {
                    "contract": DEFAULT_GUI_CONTRACT,
                    "action": DEFAULT_GUI_ACTION,
                    "cwd": str(Path.cwd()),
                }
            )
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        if not self._origin_allowed():
            self._send_json(
                {"ok": False, "errors": [{"msg": "Cross-origin POST rejected"}]},
                HTTPStatus.FORBIDDEN,
            )
            return
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json({"ok": False, "errors": [{"msg": str(exc)}]}, HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/api/validate":
            self._send_json(validate_contract_payload(payload.get("contract", payload)))
            return
        if self.path == "/api/gate":
            self._send_json(gate_payload(payload.get("contract", {}), payload.get("action", {})))
            return
        if self.path == "/api/save":
            self._send_json(save_contract_payload(payload.get("contract", {}), payload.get("path")))
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        # Keep the terminal clean; the GUI returns errors inline.
        return

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return origin in {f"http://{host}", f"https://{host}"}

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("Request JSON must be an object")
        return data

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def launch_gui(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    """Start the local GUI server and block until interrupted."""
    server = ThreadingHTTPServer((host, port), AgentContractGUIHandler)
    url = f"http://{host}:{server.server_port}/"
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    print(f"agentcontract GUI running at {url}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch the agentcontract setup GUI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="Do not open a browser automatically")
    args = parser.parse_args()
    launch_gui(host=args.host, port=args.port, open_browser=not args.no_open)


def _index_html() -> str:
    contract_json = json.dumps(DEFAULT_GUI_CONTRACT, indent=2)
    action_json = json.dumps(DEFAULT_GUI_ACTION, indent=2)
    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>agentcontract setup</title>
  <style>
    :root {{ color-scheme: dark; --bg:#0b1020; --panel:#111932; --panel2:#172347; --text:#e8edff; --muted:#9fb0d8; --accent:#7dd3fc; --good:#34d399; --bad:#fb7185; --warn:#fbbf24; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif; background: radial-gradient(circle at top left, #1d2b5f 0, var(--bg) 42%); color: var(--text); }}
    header {{ padding: 40px 44px 24px; }}
    h1 {{ font-size: 48px; line-height: 1; margin: 0 0 10px; letter-spacing: -0.05em; }}
    .tagline {{ color: var(--muted); font-size: 18px; max-width: 760px; }}
    main {{ display: grid; grid-template-columns: minmax(480px, 1.2fr) minmax(360px, .8fr); gap: 20px; padding: 0 44px 44px; }}
    .card {{ background: linear-gradient(180deg, rgba(255,255,255,.055), rgba(255,255,255,.025)); border: 1px solid rgba(255,255,255,.12); border-radius: 22px; box-shadow: 0 20px 70px rgba(0,0,0,.35); overflow: hidden; }}
    .card h2 {{ display:flex; align-items:center; justify-content:space-between; margin: 0; padding: 18px 20px; font-size: 16px; color: #dbeafe; background: rgba(255,255,255,.04); border-bottom:1px solid rgba(255,255,255,.08); }}
    textarea {{ width: 100%; min-height: 520px; resize: vertical; border: 0; outline: 0; padding: 20px; background: rgba(3,8,23,.72); color: #e5f1ff; font: 14px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    .side {{ display: grid; gap: 20px; }}
    .panel-body {{ padding: 20px; }}
    label {{ display:block; color: var(--muted); font-size: 13px; margin-bottom: 8px; }}
    input {{ width:100%; padding: 12px 14px; border-radius: 12px; border:1px solid rgba(255,255,255,.16); background:#0b1226; color:var(--text); }}
    button {{ appearance:none; border:0; border-radius: 14px; padding: 12px 16px; font-weight: 750; color:#04111f; background: var(--accent); cursor: pointer; box-shadow: 0 8px 28px rgba(125,211,252,.22); }}
    button.secondary {{ background: #24345f; color: var(--text); box-shadow:none; border:1px solid rgba(255,255,255,.12); }}
    button.good {{ background: var(--good); }}
    .buttons {{ display:flex; flex-wrap:wrap; gap: 10px; margin-top: 16px; }}
    .status {{ border-radius: 16px; padding: 14px 16px; background: #0b1226; border: 1px solid rgba(255,255,255,.12); color: var(--muted); min-height: 56px; white-space: pre-wrap; }}
    .status.ok {{ color: var(--good); border-color: rgba(52,211,153,.35); }}
    .status.bad {{ color: var(--bad); border-color: rgba(251,113,133,.35); }}
    .decision {{ font-size: 34px; letter-spacing: -.04em; font-weight: 850; }}
    .pill {{ display:inline-flex; align-items:center; gap:8px; border-radius:999px; padding:7px 11px; background: rgba(125,211,252,.12); color: var(--accent); font-size: 12px; }}
    .grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap: 12px; }}
    .mini {{ min-height: 220px; }}
    footer {{ color: var(--muted); padding: 0 44px 32px; font-size: 13px; }}
    @media (max-width: 980px) {{ main {{ grid-template-columns: 1fr; padding:0 18px 24px; }} header {{ padding:28px 18px 20px; }} h1 {{ font-size:38px; }} footer {{ padding:0 18px 28px; }} }}
  </style>
</head>
<body>
  <header>
    <div class=\"pill\">JSON-native agent governance</div>
    <h1>agentcontract setup</h1>
    <div class=\"tagline\">Create a contract, validate it, and dry-run tool calls before an AI agent touches files, networks, or paid APIs.</div>
  </header>
  <main>
    <section class=\"card\">
      <h2>1. Contract JSON <span class=\"pill\">schema v1.0</span></h2>
      <textarea id=\"contract\" spellcheck=\"false\">{contract_json}</textarea>
    </section>
    <aside class=\"side\">
      <section class=\"card\">
        <h2>2. Setup actions</h2>
        <div class=\"panel-body\">
          <label for=\"path\">Save path</label>
          <input id=\"path\" value=\"my-agent.json\" />
          <div class=\"buttons\">
            <button onclick=\"validateContract()\">Validate</button>
            <button class=\"good\" onclick=\"saveContract()\">Save contract</button>
            <button class=\"secondary\" onclick=\"resetContract()\">Reset example</button>
          </div>
          <p id=\"setupStatus\" class=\"status\">Ready. Edit the contract, then validate or save it.</p>
        </div>
      </section>
      <section class=\"card\">
        <h2>3. Try an action</h2>
        <textarea id=\"action\" class=\"mini\" spellcheck=\"false\">{action_json}</textarea>
        <div class=\"panel-body\">
          <div class=\"buttons\"><button onclick=\"runGate()\">Run gate</button></div>
          <p id=\"gateStatus\" class=\"status\">No gate decision yet.</p>
        </div>
      </section>
    </aside>
  </main>
  <footer>Run with <code>agc gui</code>. The app stays local on 127.0.0.1 and sends contract data only to this local process.</footer>
  <script>
    const defaultContract = {json.dumps(DEFAULT_GUI_CONTRACT)};
    const defaultAction = {json.dumps(DEFAULT_GUI_ACTION)};
    const contractEl = document.getElementById('contract');
    const actionEl = document.getElementById('action');
    const setupStatus = document.getElementById('setupStatus');
    const gateStatus = document.getElementById('gateStatus');
    function parseJson(el) {{ return JSON.parse(el.value); }}
    function setStatus(el, ok, text) {{ el.className = 'status ' + (ok ? 'ok' : 'bad'); el.textContent = text; }}
    function resetContract() {{ contractEl.value = JSON.stringify(defaultContract, null, 2); actionEl.value = JSON.stringify(defaultAction, null, 2); setStatus(setupStatus, true, 'Example restored.'); }}
    async function post(path, body) {{ const r = await fetch(path, {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(body) }}); return await r.json(); }}
    async function validateContract() {{
      try {{
        const data = await post('/api/validate', {{contract: parseJson(contractEl)}});
        setStatus(setupStatus, data.ok, data.ok ? `Valid contract for ${{data.contract.agent_id}}.` : JSON.stringify(data.errors, null, 2));
      }} catch (e) {{ setStatus(setupStatus, false, e.message); }}
    }}
    async function saveContract() {{
      try {{
        const data = await post('/api/save', {{contract: parseJson(contractEl), path: document.getElementById('path').value}});
        setStatus(setupStatus, data.ok, data.ok ? `Saved to ${{data.path}}` : JSON.stringify(data.errors, null, 2));
      }} catch (e) {{ setStatus(setupStatus, false, e.message); }}
    }}
    async function runGate() {{
      try {{
        const data = await post('/api/gate', {{contract: parseJson(contractEl), action: parseJson(actionEl)}});
        if (!data.ok) {{ setStatus(gateStatus, false, `${{data.stage}} error:\\n${{JSON.stringify(data.errors, null, 2)}}`); return; }}
        const result = data.result;
        const ok = result.decision === 'allow' || result.decision === 'require_approval';
        const violations = result.violations.length ? '\\n' + result.violations.map(v => `• ${{v.field}}: ${{v.message}}`).join('\\n') : '';
        setStatus(gateStatus, ok, `${{result.decision.toUpperCase()}}${{violations}}`);
        gateStatus.innerHTML = `<div class=\"decision\">${{result.decision.toUpperCase()}}</div>${{violations ? '<pre>'+violations+'</pre>' : '<span>Action passes this contract.</span>'}}`;
      }} catch (e) {{ setStatus(gateStatus, false, e.message); }}
    }}
  </script>
</body>
</html>"""


if __name__ == "__main__":
    main()
