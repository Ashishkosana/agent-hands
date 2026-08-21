"""A thin conversational front door over the capability API.

This stands in for the AI agent. It uses an LLM for ONE thing only — mapping a
natural-language request to a single capability plus its typed args — then calls
the API and reports the structured result in plain language. The LLM is never in
the replay path; the deterministic engine still does the work behind the API.

Credentials are never spoken by the user or the model: the operator id is the
chatbot's own session identity, and the password is injected by the API from the
environment. Sensitive params are excluded from the tool schema the model sees.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from flask import Flask, jsonify, render_template_string
from flask import request as flask_request

from hands.llm import client_from_env

API = os.environ.get("HANDS_API_URL", "http://127.0.0.1:8100")
SESSION_OPERATOR = os.environ.get("HANDS_CHAT_OPERATOR", "teller1")
# The chatbot is a logged-in teller at ONE institution; it only exposes that
# institution's capabilities so requests route unambiguously.
SESSION_APP = os.environ.get("HANDS_CHAT_APP", "meridian-core")

# Control tools let the router decline or ask, instead of forcing a bad guess.
CONTROL_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "ask_clarification",
            "description": "Ask the user for a required detail that was not provided.",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "no_capability",
            "description": "No available capability matches the request.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    },
]

_SYSTEM = (
    "You route a user's banking request to exactly one capability tool, filling its typed "
    "arguments from the request. Never invent an argument you were not given — call "
    "ask_clarification for a missing required detail. If no capability fits, call no_capability."
)


def _get(path: str) -> Any:
    with urllib.request.urlopen(API + path) as resp:
        return json.load(resp)


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        API + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)  # type: ignore[no-any-return]
    except urllib.error.HTTPError as exc:
        return json.load(exc)  # type: ignore[no-any-return]


def _contracts_by_name() -> dict[str, Any]:
    return {c["name"]: c for c in _get("/capabilities")}


def _agent_tools(contracts: dict[str, Any]) -> list[dict[str, Any]]:
    """Tool-schemas for THIS institution's capabilities only, minus operator_id
    (that is the chatbot's session identity, not something the user/model gives)."""
    allowed = {n for n, c in contracts.items() if c.get("app") == SESSION_APP}
    tools = [t for t in _get("/capabilities?format=tools") if t["function"]["name"] in allowed]
    for tool in tools:
        params = tool["function"]["parameters"]
        params["properties"].pop("operator_id", None)
        params["required"] = [r for r in params.get("required", []) if r != "operator_id"]
    return tools


def _route(utterance: str, tools: list[dict[str, Any]]) -> Any:
    client = client_from_env()
    turn = client.complete(
        [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": utterance}],
        tools + CONTROL_TOOLS,
    )
    return turn.tool_calls[0] if turn.tool_calls else None


def _render(envelope: dict[str, Any]) -> str:
    result = envelope["result"]
    kind = result["result"]
    if kind == "success":
        outs = ", ".join(f"{k} = {v}" for k, v in result["outputs"].items())
        return f"Done. {outs}"
    if kind == "business_outcome":
        return f"{result['description']} (outcome: {result['code']})"
    if kind == "failure":
        rep = result["report"]
        return f"It stopped at step {rep['step_id']} — {rep['observed']}"
    if kind == "precondition_failed":
        return f"The system wasn't in the right state: {result['unmet']}"
    if kind == "policy_violation":
        return f"Blocked by a guardrail: {result['detail']}"
    return json.dumps(result)


def handle(utterance: str) -> str:
    contracts = _contracts_by_name()
    call = _route(utterance, _agent_tools(contracts))
    if call is None:
        return "I couldn't understand that request."
    if call.name == "ask_clarification":
        return call.arguments.get("question", "Could you clarify?")
    if call.name == "no_capability":
        return "I don't have a capability for that. " + call.arguments.get("reason", "")
    args = dict(call.arguments)
    # Inject the session operator only where the capability declares it.
    if "operator_id" in contracts.get(call.name, {}).get("parameters", {}):
        args["operator_id"] = SESSION_OPERATOR
    print(f"  (running '{call.name}'...)", file=sys.stderr)
    envelope = _post(f"/capabilities/{call.name}/invoke", {"params": args})
    if "result" not in envelope:
        return envelope.get("error", "invocation error")
    return _render(envelope)


_DASH_URL = os.environ.get("HANDS_DASH_URL", "http://127.0.0.1:8200/")

_CHAT_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>MERIDIAN Console</title><style>
 :root{--ink:#101828;--muted:#475467;--line:#eaecf0;--canvas:#f8fafc;--card:#ffffff;
       --accent:#4f46e5;--shadow:0 1px 2px rgba(16,24,40,.05),0 1px 3px rgba(16,24,40,.08)}
 *{box-sizing:border-box}
 body{margin:0;background:var(--canvas);color:var(--ink);
      font:15px/1.55 -apple-system,BlinkMacSystemFont,"Inter","Segoe UI",sans-serif;display:flex;height:100vh}
 #left{width:40%;min-width:380px;display:flex;flex-direction:column;background:var(--card);
       border-right:1px solid var(--line)}
 #right{flex:1;display:flex;flex-direction:column}
 #head{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center}
 .mark{width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,#4f46e5,#7c3aed);
       display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:15px;flex:none}
 #head b{font-size:16px;letter-spacing:-.01em}
 #head small{color:var(--muted);font-weight:400;display:block;margin-top:1px;font-size:12.5px}
 #log{flex:1;overflow:auto;padding:24px;display:flex;flex-direction:column;gap:12px;background:#fcfcfd}
 .m{max-width:82%;padding:11px 15px;border-radius:16px;white-space:pre-wrap;font-size:14.5px;box-shadow:var(--shadow)}
 .you{align-self:flex-end;background:var(--accent);color:#fff;border-bottom-right-radius:5px}
 .bot{align-self:flex-start;background:var(--card);border:1px solid var(--line);border-bottom-left-radius:5px}
 .think{color:var(--muted);font-style:italic;box-shadow:none;background:transparent;border:0}
 #bar{display:flex;gap:10px;padding:16px 24px;border-top:1px solid var(--line);background:var(--card)}
 #msg{flex:1;background:var(--card);border:1px solid #d0d5dd;border-radius:12px;color:var(--ink);
      padding:12px 15px;font-size:14.5px;outline:none;box-shadow:var(--shadow)}
 #msg:focus{border-color:var(--accent);box-shadow:0 0 0 4px rgba(79,70,229,.12)}
 button{background:var(--accent);color:#fff;border:0;border-radius:12px;padding:0 22px;font-size:14.5px;
        font-weight:600;cursor:pointer;box-shadow:var(--shadow)}
 button:hover{background:#4338ca}
 .rhead{padding:13px 20px;border-bottom:1px solid var(--line);color:var(--muted);font-size:13px;background:var(--card)}
 iframe{flex:1;border:0;background:var(--canvas)}
</style></head><body>
 <div id="left">
   <div id="head"><div class="mark">◆</div>
     <div><b>MERIDIAN Assistant</b><small>ask in plain English — e.g. "check the balance for member 100234"</small></div>
   </div>
   <div id="log"></div>
   <div id="bar"><input id="msg" placeholder="type a request…" autofocus><button onclick="send()">Send</button></div>
 </div>
 <div id="right">
   <div class="rhead">Live runs &amp; evidence — every request you make appears here</div>
   <iframe id="dash" src="__DASH__"></iframe>
 </div>
<script>
 const log=document.getElementById('log'), msg=document.getElementById('msg'), dash=document.getElementById('dash');
 function add(text,cls){const d=document.createElement('div');d.className='m '+cls;d.textContent=text;log.appendChild(d);log.scrollTop=log.scrollHeight;return d;}
 async function send(){
   const t=msg.value.trim(); if(!t)return; add(t,'you'); msg.value='';
   const w=add('…working (driving the bank)…','bot think');
   try{const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:t})});
       const d=await r.json(); w.remove(); add(d.reply,'bot'); dash.src=dash.src;}
   catch(e){w.remove(); add('(something went wrong)','bot');}
 }
 msg.addEventListener('keydown',e=>{if(e.key==='Enter')send();});
</script></body></html>""".replace("__DASH__", _DASH_URL)


def create_web() -> Flask:
    app = Flask("hands-chatbot-web")

    @app.get("/")
    def index() -> str:
        return render_template_string(_CHAT_PAGE)

    @app.post("/chat")
    def chat() -> Any:
        message = (flask_request.get_json(silent=True) or {}).get("message", "")
        return jsonify({"reply": handle(message)})

    return app


def main() -> None:  # pragma: no cover - convenience runner
    if "--web" in sys.argv:
        port = 8300
        if "--port" in sys.argv:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        print(f"MERIDIAN Assistant (web) → http://127.0.0.1:{port}/")
        create_web().run(port=port)
        return
    if len(sys.argv) > 1:
        print("bot> " + handle(" ".join(sys.argv[1:])))
        return
    print("Fairview chatbot (Ctrl-C to quit). e.g. \"check the balance for member 100234\"")
    try:
        while True:
            utterance = input("you> ").strip()
            if utterance:
                print("bot> " + handle(utterance))
    except (EOFError, KeyboardInterrupt):
        print()


if __name__ == "__main__":  # pragma: no cover
    main()
