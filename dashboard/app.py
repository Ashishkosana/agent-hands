"""A read-only dashboard to WATCH the system work.

Shows the capability catalog and the run history (discovery AND replay) with
each run's inputs, structured result, status, and evidence. It reads the
already-masked runs/*/trace.jsonl audit logs — it never drives anything and
imports only hands.artifact (never the replay engine, the model client, or
discovery), so a reviewer can watch and debug without any risk of side effects.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from flask import Flask, abort, render_template_string, send_from_directory

from hands.artifact import dump_capability, load_capability, risk_review_valid

_STATUS_COLOR = {
    "success": "#3fb950",
    "business_outcome": "#58a6ff",
    "recorded": "#3fb950",
    "failure": "#f85149",
    "precondition_failed": "#d29922",
    "policy_violation": "#d29922",
    "failed": "#f85149",
    "unknown": "#8b949e",
}


def _events(run_dir: Path) -> list[dict[str, Any]]:
    tf = run_dir / "trace.jsonl"
    if not tf.exists():
        return []
    out = []
    for line in tf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _first(events: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((e for e in events if e.get("event") == name), None)


def _summarize_run(run_dir: Path) -> dict[str, Any] | None:
    events = _events(run_dir)
    if not events:
        return None
    started = _first(events, "run_started")
    finished = _first(events, "run_finished")
    disc = _first(events, "discovery_started")

    if started is not None:  # a replay run
        kind = "replay"
        capability = started.get("capability", "?")
        sha = started.get("artifact_sha256", "")
        params = started.get("params", {})
        result = (finished or {}).get("result", {}) if finished else {}
        status = result.get("result", "unknown") if isinstance(result, dict) else "unknown"
        outputs = result.get("outputs") if isinstance(result, dict) else None
        code = result.get("code") if isinstance(result, dict) else None
    elif disc is not None:  # a discovery run
        kind = "discovery"
        capability = disc.get("capability", "?")
        sha = ""
        params = {}
        status = "recorded" if _first(events, "discovery_distilled") else "failed"
        outputs = None
        code = disc.get("leg")
    else:
        return None

    evidence = [p.name for p in run_dir.iterdir() if p.suffix in (".png", ".txt") and p.name != "trace.jsonl"]
    return {
        "id": run_dir.name,
        "kind": kind,
        "capability": capability,
        "when": run_dir.name[:15].replace("T", " "),
        "status": status,
        "code": code,
        "outputs": outputs,
        "params": params,
        "sha": sha,
        "events": len(events),
        "evidence": evidence,
    }


def _catalog(gen_dir: Path, app: str | None = None) -> list[dict[str, Any]]:
    caps = []
    for path in sorted(gen_dir.glob("*.json")):
        try:
            cap = load_capability(path)
        except (OSError, ValueError):
            continue
        if app is not None and cap.target.app != app:
            continue  # scope the shown catalog to one application
        sha = hashlib.sha256(dump_capability(cap).encode()).hexdigest()
        caps.append({
            "name": cap.name,
            "version": cap.version,
            "description": cap.description,
            "params": list(cap.parameters),
            "outputs": list(cap.outputs),
            "outcomes": list(cap.outcomes),
            "risky_steps": [s.id for s in cap.steps if s.risk == "risky"],
            "signed": risk_review_valid(cap),
            "sha": sha,
        })
    return caps


def create_dashboard(
    gen_dir: str | Path = "capabilities/generated",
    runs_dir: str | Path = "runs",
) -> Flask:
    app = Flask("hands-dashboard")
    gen = Path(gen_dir)
    runs = Path(runs_dir)
    only = os.environ.get("HANDS_APP")  # scope the shown catalog + runs to one application

    @app.get("/")
    def index() -> str:
        catalog = _catalog(gen, only)
        current_shas = {c["sha"] for c in catalog}
        names = {c["name"] for c in catalog}
        run_dirs = sorted((d for d in runs.iterdir() if d.is_dir()), reverse=True) if runs.exists() else []
        summaries = [s for s in (_summarize_run(d) for d in run_dirs) if s is not None]
        if only is not None:
            summaries = [s for s in summaries if s["capability"] in names]
        for s in summaries:
            s["drift"] = "current" if (s["sha"] and s["sha"] in current_shas) else ("drifted" if s["sha"] else "")
        return render_template_string(_INDEX, catalog=catalog, runs=summaries, color=_STATUS_COLOR)

    @app.get("/run/<run_id>")
    def run_detail(run_id: str) -> str:
        run_dir = runs / run_id
        if not run_dir.is_dir() or ".." in run_id:
            abort(404)
        return render_template_string(
            _DETAIL, run_id=run_id, events=_events(run_dir),
            summary=_summarize_run(run_dir), color=_STATUS_COLOR,
        )

    @app.get("/run/<run_id>/evidence/<path:filename>")
    def evidence(run_id: str, filename: str) -> Any:
        run_dir = runs / run_id
        if not run_dir.is_dir() or ".." in run_id or ".." in filename:
            abort(404)
        return send_from_directory(run_dir, filename)

    return app


_CSS = """
<style>
 body{margin:0;background:#0d1117;color:#e6edf3;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:28px}
 h1{font-size:22px;margin:0 0 2px} h2{font-size:15px;color:#8b949e;margin:26px 0 12px;text-transform:uppercase;letter-spacing:.5px}
 a{color:#58a6ff;text-decoration:none} a:hover{text-decoration:underline}
 .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
 .card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:16px}
 .card h3{margin:0 0 6px;font-size:15px} .muted{color:#8b949e;font-size:12.5px}
 .pill{display:inline-block;font-size:11px;padding:2px 8px;border-radius:20px;margin:2px 4px 2px 0;background:#21262d;color:#c9d1d9}
 .risky{background:rgba(248,81,73,.15);color:#f85149} .signed{background:rgba(63,185,80,.15);color:#3fb950}
 table{width:100%;border-collapse:collapse;font-size:13px} th{text-align:left;color:#8b949e;font-weight:600;padding:8px;border-bottom:1px solid #30363d}
 td{padding:8px;border-bottom:1px solid #21262d;vertical-align:top}
 .badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11.5px;font-weight:600;color:#0d1117}
 .k{font-family:ui-monospace,monospace;font-size:12px;color:#8b949e}
 .drift-current{color:#3fb950} .drift-drifted{color:#d29922}
 pre{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px;overflow:auto;font-size:12px}
 .ev{border-left:2px solid #30363d;padding:4px 0 4px 14px;margin-left:6px} .evname{color:#58a6ff;font-family:ui-monospace,monospace}
 img{max-width:100%;border:1px solid #30363d;border-radius:8px}
</style>
"""

_INDEX = _CSS + """
<h1>agent-hands · Run Dashboard</h1>
<div class="muted">the capability catalog and every discovery + replay run, with results and evidence</div>

<h2>Capabilities</h2>
<div class="cards">
{% for c in catalog %}
  <div class="card">
    <h3>{{c.name}} <span class="muted">v{{c.version}}</span></h3>
    <div class="muted">{{c.description}}</div>
    <div style="margin-top:8px">
      {% for p in c.params %}<span class="pill">{{p}}</span>{% endfor %}
    </div>
    <div style="margin-top:6px">
      {% for o in c.outputs %}<span class="pill">→ {{o}}</span>{% endfor %}
      {% for o in c.outcomes %}<span class="pill">⚑ {{o}}</span>{% endfor %}
    </div>
    <div style="margin-top:8px">
      {% if c.risky_steps %}<span class="pill risky">risky: {{c.risky_steps|join(', ')}}</span>{% endif %}
      {% if c.signed %}<span class="pill signed">signed ✓</span>{% else %}<span class="pill">unsigned</span>{% endif %}
    </div>
    <div class="k" style="margin-top:8px">sha {{c.sha[:16]}}…</div>
  </div>
{% endfor %}
</div>

<h2>Run history ({{runs|length}})</h2>
<table>
 <tr><th>When (UTC)</th><th>Capability</th><th>Kind</th><th>Status</th><th>Result</th><th>Contract</th><th></th></tr>
{% for r in runs %}
 <tr>
   <td class="k">{{r.when}}</td>
   <td>{{r.capability}}</td>
   <td class="muted">{{r.kind}}</td>
   <td><span class="badge" style="background:{{color.get(r.status,'#8b949e')}}">{{r.status}}</span></td>
   <td class="muted">
     {% if r.outputs %}{{r.outputs}}{% elif r.code %}{{r.code}}{% endif %}
   </td>
   <td>{% if r.drift=='current' %}<span class="drift-current">✓ current</span>{% elif r.drift=='drifted' %}<span class="drift-drifted">⚠ drifted</span>{% endif %}</td>
   <td><a href="/run/{{r.id}}">view →</a></td>
 </tr>
{% endfor %}
</table>
"""

_DETAIL = _CSS + """
<div><a href="/">← all runs</a></div>
<h1>{{summary.capability if summary else run_id}}</h1>
<div class="muted k">{{run_id}}</div>
{% if summary %}
<div style="margin:10px 0">
  <span class="badge" style="background:{{color.get(summary.status,'#8b949e')}}">{{summary.status}}</span>
  {% if summary.code %}<span class="pill">{{summary.code}}</span>{% endif %}
  {% if summary.outputs %}<span class="pill">{{summary.outputs}}</span>{% endif %}
</div>
<div class="muted">inputs (secrets masked): <span class="k">{{summary.params}}</span></div>
{% if summary.sha %}<div class="muted k">contract sha {{summary.sha[:24]}}…</div>{% endif %}
{% endif %}

{% if summary and summary.evidence %}
<h2>Evidence</h2>
{% for f in summary.evidence %}
  {% if f.endswith('.png') %}<img src="/run/{{run_id}}/evidence/{{f}}">
  {% else %}<div><a href="/run/{{run_id}}/evidence/{{f}}">{{f}}</a></div>{% endif %}
{% endfor %}
{% endif %}

<h2>Trace ({{events|length}} events)</h2>
<div>
{% for e in events %}
  <div class="ev"><span class="evname">{{e.event}}</span>
    <span class="muted k">{{e.ts[11:23]}}</span>
    <span class="muted">{% for k,v in e.items() %}{% if k not in ('ts','event') %}{{k}}={{v}}  {% endif %}{% endfor %}</span>
  </div>
{% endfor %}
</div>
"""


def main() -> None:  # pragma: no cover - convenience runner
    import argparse

    parser = argparse.ArgumentParser(description="serve the run dashboard")
    parser.add_argument("--port", type=int, default=8200)
    parser.add_argument("--gen-dir", default="capabilities/generated")
    parser.add_argument("--runs-dir", default="runs")
    args = parser.parse_args()
    create_dashboard(args.gen_dir, args.runs_dir).run(port=args.port)


if __name__ == "__main__":  # pragma: no cover
    main()
