"""A read-only TRUST dashboard: watch the system work, in plain language.

Not a log viewer — a trust view. For every automated run it answers, at a
glance, the four questions a non-engineer (or an examiner) actually asks:
what happened, was any AI involved, who approved the recipe, and has anything
been tampered with since.

It never drives anything: it reads the already-masked runs/*/trace.jsonl audit
logs and the capability artifacts. It imports only model-free leaf modules
(hands.artifact for the schema, hands.trace for the hash-chain verifier) —
never the replay engine, the model client, or discovery — so it cannot act,
cannot leak, and cannot bypass a guardrail.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flask import Flask, abort, render_template_string, send_from_directory

from hands.artifact import dump_capability, load_capability, risk_review_valid
from hands.trace import is_chained, verify_chain

# Plain-word badges: every status a non-engineer understands on sight.
# (label, text color, soft background) — the light-fintech badge idiom.
_PLAIN_STATUS = {
    "success": ("Completed", "#067647", "#ecfdf3"),
    "business_outcome": ("Clean answer", "#175cd3", "#eff8ff"),
    "failure": ("Stopped safely", "#b42318", "#fef3f2"),
    "precondition_failed": ("Not ready", "#b54708", "#fffaeb"),
    "policy_violation": ("Blocked", "#b54708", "#fffaeb"),
    "recorded": ("Recipe recorded", "#067647", "#ecfdf3"),
    "failed": ("Recording failed", "#b42318", "#fef3f2"),
    "unknown": ("Unknown", "#475467", "#f2f4f7"),
}

# Event names that would indicate a model was involved in a run.
_MODEL_MARKERS = ("llm", "model", "planner", "discovery")


def _title(capability: str) -> str:
    """Human title for a capability name: meridian_member_balance -> Member balance."""
    words = capability.replace("meridian_", "").replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else capability


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


def _plain_outputs(outputs: dict[str, Any]) -> str:
    """'checking_balance': '«masked»' -> 'checking balance: (masked)'."""
    parts = []
    for key, value in outputs.items():
        shown = "(masked)" if str(value) == "«masked»" else str(value)
        parts.append(f"{key.replace('_', ' ')}: {shown}")
    return " · ".join(parts)


def _plain_sentence(kind: str, status: str, outputs: dict[str, Any] | None, code: Any) -> str:
    """One sentence anyone can read, per run."""
    if kind == "discovery":
        return (
            "Learning run — the AI worked out this recipe (the one place AI is used)"
            if status == "recorded"
            else "Learning run that didn't finish — nothing was recorded"
        )
    if status == "success":
        return _plain_outputs(outputs or {}) or "Task completed"
    if status == "business_outcome":
        human = str(code or "outcome").replace("_", " ").lower()
        return f"Real answer: {human} (not an error)"
    if status == "failure":
        return "Something was off — it stopped without acting wrongly, evidence saved"
    if status == "policy_violation":
        return "A safety rule refused this run"
    if status == "precondition_failed":
        return "The system wasn't in the right state to start"
    return "—"


def _step_timings(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-step wall-clock benchmarks, computed from the trace timestamps:
    each step's duration runs from its step_started to its postconditions_met
    (or to the next step's start, whichever the trace shows first)."""
    def _ts(e: dict[str, Any]) -> datetime | None:
        try:
            return datetime.fromisoformat(str(e.get("ts")))
        except (TypeError, ValueError):
            return None

    timings: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    started_at: datetime | None = None
    for e in events:
        name = e.get("event")
        if name == "step_started":
            if current is not None and started_at is not None and current.get("ms") is None:
                end = _ts(e)
                current["ms"] = int((end - started_at).total_seconds() * 1000) if end else None
            current = {"step": e.get("step"), "intent": e.get("intent"),
                       "risk": e.get("risk"), "ms": None}
            started_at = _ts(e)
            timings.append(current)
        elif name == "postconditions_met" and current is not None and started_at is not None:
            end = _ts(e)
            if end is not None:
                current["ms"] = int((end - started_at).total_seconds() * 1000)
    max_ms = max((t["ms"] for t in timings if t.get("ms")), default=0) or 1
    for t in timings:
        t["bar"] = int(100 * (t["ms"] or 0) / max_ms)
    return timings


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
    elif disc is not None:  # a discovery (learning) run
        kind = "discovery"
        capability = disc.get("capability", "?")
        sha = ""
        params = {}
        status = "recorded" if _first(events, "discovery_distilled") else "failed"
        outputs = None
        code = disc.get("leg")
    else:
        return None

    # The differentiator: was ANY model involved in this run? Counted from the
    # trace itself, not asserted.
    model_events = sum(
        1 for e in events if any(m in str(e.get("event", "")) for m in _MODEL_MARKERS)
    )

    # Tamper evidence, recomputed from the same hash chain `hands explain` uses.
    if is_chained(run_dir):
        log_state = "intact" if verify_chain(run_dir) else "broken"
    else:
        log_state = "legacy"

    evidence = [
        p.name
        for p in run_dir.iterdir()
        if p.suffix in (".png", ".txt") and p.name != "trace.jsonl"
    ]
    plain_status, color, badge_bg = _PLAIN_STATUS.get(status, _PLAIN_STATUS["unknown"])
    return {
        "id": run_dir.name,
        "kind": kind,
        "capability": capability,
        "title": _title(str(capability)),
        "when": run_dir.name[:15].replace("T", " "),
        "status": status,
        "plain_status": plain_status,
        "color": color,
        "badge_bg": badge_bg,
        "plain": _plain_sentence(kind, status, outputs, code),
        "code": code,
        "outputs": outputs,
        "params": params,
        "sha": sha,
        "model_events": model_events,
        "log_state": log_state,
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
            "title": _title(cap.name),
            "version": cap.version,
            "description": cap.description,
            "params": list(cap.parameters),
            "outputs": list(cap.outputs),
            "outcomes": list(cap.outcomes),
            "risky_steps": [s.id for s in cap.steps if s.risk == "risky"],
            # Display heuristic only (the gate uses the real labels): flag the
            # card as irreversible when risk goes beyond the sign-on POST.
            "irreversible": [
                s.id for s in cap.steps
                if s.risk == "risky" and "sign on" not in s.intent.lower()
            ],
            "signed": risk_review_valid(cap),
            "signed_by": cap.risk_review.reviewed_by,
            "recorded_by": cap.recorded_by,
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

    def _load(run_id: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        catalog = _catalog(gen, only)
        names = {c["name"] for c in catalog}
        approvals = {c["name"]: c for c in catalog}
        run_dirs = (
            sorted((d for d in runs.iterdir() if d.is_dir()), reverse=True)
            if runs.exists()
            else []
        )
        if run_id is not None:
            run_dirs = [d for d in run_dirs if d.name == run_id]
        summaries = [s for s in (_summarize_run(d) for d in run_dirs) if s is not None]
        if only is not None:
            summaries = [s for s in summaries if s["capability"] in names]
        current_shas = {c["sha"] for c in catalog}
        for s in summaries:
            s["drift"] = (
                "current" if (s["sha"] and s["sha"] in current_shas)
                else ("drifted" if s["sha"] else "")
            )
            cap = approvals.get(str(s["capability"]))
            s["approved_by"] = cap["signed_by"] if cap and cap["signed"] else None
        return catalog, summaries

    @app.get("/")
    def index() -> str:
        catalog, summaries = _load()
        replays = [s for s in summaries if s["kind"] == "replay"]
        today = datetime.now(tz=UTC).strftime("%Y%m%d")
        cards = {
            "runs_today": sum(1 for s in replays if s["id"].startswith(today)),
            "runs_total": len(replays),
            "answered": sum(
                1 for s in replays if s["status"] in ("success", "business_outcome")
            ),
            "ai_calls": sum(s["model_events"] for s in replays),
            "signed": sum(1 for c in catalog if c["signed"]),
            "caps": len(catalog),
        }
        return render_template_string(_INDEX, catalog=catalog, runs=summaries, cards=cards)

    @app.get("/run/<run_id>")
    def run_detail(run_id: str) -> str:
        run_dir = runs / run_id
        if not run_dir.is_dir() or ".." in run_id:
            abort(404)
        _, summaries = _load(run_id)
        events = _events(run_dir)
        return render_template_string(
            _DETAIL,
            run_id=run_id,
            events=events,
            timings=_step_timings(events),
            summary=summaries[0] if summaries else None,
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
 :root{--ink:#101828;--muted:#475467;--faint:#98a2b3;--line:#eaecf0;--canvas:#f8fafc;
       --card:#ffffff;--accent:#4f46e5;--accent-soft:#eef2ff;
       --ok:#067647;--ok-bg:#ecfdf3;--warn:#b54708;--warn-bg:#fffaeb;--risk:#b42318;--risk-bg:#fef3f2;
       --shadow:0 1px 2px rgba(16,24,40,.05),0 1px 3px rgba(16,24,40,.08)}
 *{box-sizing:border-box}
 body{margin:0;background:var(--canvas);color:var(--ink);
      font:14px/1.55 -apple-system,BlinkMacSystemFont,"Inter","Segoe UI",sans-serif;padding:32px 36px}
 h1{font-size:22px;font-weight:700;letter-spacing:-.02em;margin:0 0 2px}
 h2{font-size:12px;color:var(--faint);margin:30px 0 12px;text-transform:uppercase;letter-spacing:.08em;font-weight:600}
 a{color:var(--accent);text-decoration:none;font-weight:500} a:hover{text-decoration:underline}
 .muted{color:var(--muted);font-size:13px}
 .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-top:18px}
 .stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;box-shadow:var(--shadow)}
 .stat .n{font-size:26px;font-weight:700;letter-spacing:-.02em} .stat .l{color:var(--muted);font-size:12.5px;margin-top:3px}
 .good{color:var(--ok)}
 .capgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;box-shadow:var(--shadow)}
 .card h3{margin:0 0 4px;font-size:15px;font-weight:600}
 .pill{display:inline-block;font-size:11.5px;font-weight:500;padding:2px 9px;border-radius:999px;margin:2px 4px 2px 0;
       background:#f2f4f7;color:var(--muted);border:1px solid var(--line)}
 .ok{background:var(--ok-bg);color:var(--ok);border-color:#abefc6}
 .warn{background:var(--warn-bg);color:var(--warn);border-color:#fedf89}
 .risk{background:var(--risk-bg);color:var(--risk);border-color:#fecdca}
 table{width:100%;border-collapse:separate;border-spacing:0;font-size:13px;background:var(--card);
       border:1px solid var(--line);border-radius:14px;overflow:hidden;box-shadow:var(--shadow)}
 th{text-align:left;color:var(--muted);font-weight:600;font-size:12px;padding:11px 14px;
    background:#f9fafb;border-bottom:1px solid var(--line)}
 td{padding:12px 14px;border-bottom:1px solid var(--line);vertical-align:top}
 tr:last-child td{border-bottom:0}
 .badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid transparent}
 .k{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:12px;color:var(--faint)}
 .chip{display:inline-block;font-size:11.5px;font-weight:500;padding:1px 9px;border-radius:999px;margin:1px 3px 1px 0;
       white-space:nowrap;background:#f2f4f7;color:var(--muted);border:1px solid var(--line)}
 .legend{background:var(--accent-soft);border:1px solid #e0e7ff;border-radius:12px;padding:12px 16px;
         font-size:12.5px;color:var(--muted);margin-top:14px}
 .legend b{color:var(--ink);font-weight:600}
 .ev{border-left:2px solid var(--line);padding:5px 0 5px 14px;margin-left:6px}
 .evname{color:var(--accent);font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:12.5px}
 img{max-width:100%;border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow)}
 .brand{display:flex;align-items:center;gap:10px;margin-bottom:6px}
 .mark{width:30px;height:30px;border-radius:9px;background:linear-gradient(135deg,#4f46e5,#7c3aed);
       display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:14px}
</style>
"""

_TRUST_CHIPS = """
  {% if r.kind == 'replay' %}
    {% if r.model_events == 0 %}<span class="chip ok">No AI used ✓</span>
    {% else %}<span class="chip warn">AI events: {{r.model_events}}</span>{% endif %}
    {% if r.drift == 'current' %}
      {% if r.approved_by %}<span class="chip ok">Approved by {{r.approved_by}} ✓</span>
      {% else %}<span class="chip warn">Not approved</span>{% endif %}
      <span class="chip ok">Recipe unchanged ✓</span>
    {% elif r.drift == 'drifted' %}
      <span class="chip warn">Recipe changed since — approval unknown for this version ⚠</span>
    {% endif %}
    {% if r.log_state == 'intact' %}<span class="chip ok">Log tamper-evident ✓</span>
    {% elif r.log_state == 'broken' %}<span class="chip risk">Log MODIFIED ⚠</span>{% endif %}
  {% else %}
    <span class="chip warn">AI used here (learning run)</span>
  {% endif %}
  {% if r.evidence %}<span class="chip"><a href="/run/{{r.id}}">evidence</a></span>{% endif %}
"""

_INDEX = _CSS + """
<div class="brand"><div class="mark">◆</div><h1>Trust Dashboard</h1></div>
<div class="muted">every automated run on the banking system — what happened, whether AI was involved, who approved it, and whether the record has stayed intact (tamper-evident)</div>

<div class="cards">
  <div class="stat"><div class="n">{{cards.runs_today}} <span class="muted">/ {{cards.runs_total}}</span></div><div class="l">runs today / total</div></div>
  <div class="stat"><div class="n">{{cards.answered}} <span class="muted">/ {{cards.runs_total}}</span></div><div class="l">runs that returned a clean result</div></div>
  <div class="stat"><div class="n {{'good' if cards.ai_calls == 0 else ''}}">{{cards.ai_calls}}{{' ✓' if cards.ai_calls == 0 else ''}}</div><div class="l">AI calls during production runs</div></div>
  <div class="stat"><div class="n">{{cards.signed}} <span class="muted">/ {{cards.caps}}</span></div><div class="l">recipes human-approved</div></div>
</div>

<h2>What this system can do</h2>
<div class="capgrid">
{% for c in catalog %}
  <div class="card">
    <h3>{{c.title}} <span class="muted">v{{c.version}}</span></h3>
    <div class="muted">{{c.description}}</div>
    <div style="margin-top:8px">
      {% if c.signed %}<span class="pill ok">approved by {{c.signed_by}} ✓</span>{% else %}<span class="pill warn">awaiting approval</span>{% endif %}
      {% if c.recorded_by %}<span class="pill">recorded by {{c.recorded_by}}</span>
      {% elif c.signed %}<span class="pill warn">maker not recorded — four-eyes not verifiable</span>{% endif %}
      {% if c.irreversible %}<span class="pill risk">moves money / irreversible</span>{% endif %}
    </div>
    <div style="margin-top:6px">
      {% for p in c.params %}<span class="pill">needs: {{p.replace('_',' ')}}</span>{% endfor %}
      {% for o in c.outputs %}<span class="pill">returns: {{o.replace('_',' ')}}</span>{% endfor %}
    </div>
    <div class="k" style="margin-top:8px">recipe fingerprint {{c.sha[:16]}}…</div>
  </div>
{% endfor %}
</div>

<h2>Every run ({{runs|length}})</h2>
<table>
 <tr><th>When (UTC)</th><th>Task</th><th>What happened</th><th>Trust checks</th><th></th></tr>
{% for r in runs %}
 <tr>
   <td class="k">{{r.when}}</td>
   <td>{{r.title}}<div class="muted">{{'learning run' if r.kind == 'discovery' else ''}}</div></td>
   <td><span class="badge" style="background:{{r.badge_bg}};color:{{r.color}}">{{r.plain_status}}</span>
       <div class="muted" style="margin-top:3px">{{r.plain}}</div></td>
   <td>""" + _TRUST_CHIPS + """</td>
   <td><a href="/run/{{r.id}}">details →</a></td>
 </tr>
{% endfor %}
</table>

<div class="legend">
 <b>Completed</b> — the task worked and returned its answer &nbsp;·&nbsp;
 <b>Clean answer</b> — a real-world answer like “no such member” (not an error) &nbsp;·&nbsp;
 <b>Stopped safely</b> — something was off, so it stopped without acting wrongly and saved evidence &nbsp;·&nbsp;
 <b>Blocked</b> — a safety rule refused the run &nbsp;·&nbsp;
 <b>No AI used ✓</b> — zero model calls in this run, counted from the tamper-evident log
</div>
"""

_DETAIL = _CSS + """
<div><a href="/">← all runs</a></div>
{% if summary %}
<h1>{{summary.title}}</h1>
<div class="muted k">{{run_id}}</div>
<div style="margin:10px 0 4px">
  <span class="badge" style="background:{{summary.badge_bg}};color:{{summary.color}}">{{summary.plain_status}}</span>
  <span class="muted" style="margin-left:8px">{{summary.plain}}</span>
</div>
<div style="margin:6px 0">{% set r = summary %}""" + _TRUST_CHIPS + """</div>
<div class="muted">inputs (secrets masked): <span class="k">{{summary.params}}</span></div>
{% if summary.sha %}<div class="muted k">recipe fingerprint {{summary.sha[:24]}}…</div>{% endif %}
{% else %}<h1>{{run_id}}</h1>{% endif %}

{% if timings %}
<h2>Step timing benchmarks</h2>
<table style="max-width:760px">
 <tr><th>Step</th><th>What it did</th><th style="width:90px">Time</th><th style="width:220px"></th></tr>
{% for t in timings %}
 <tr>
   <td class="k">{{t.step}}{% if t.risk == 'risky' %} <span class="pill risk">risky</span>{% endif %}</td>
   <td class="muted">{{t.intent}}</td>
   <td class="k">{{t.ms if t.ms is not none else '—'}}{{' ms' if t.ms is not none else ''}}</td>
   <td><div style="height:8px;border-radius:4px;background:linear-gradient(90deg,#4f46e5,#7c3aed);width:{{t.bar}}%"></div></td>
 </tr>
{% endfor %}
</table>
{% endif %}

{% if summary and summary.evidence %}
<h2>Evidence (screenshots &amp; DOM snapshots)</h2>
{% for f in summary.evidence %}
  {% if f.endswith('.png') %}<img src="/run/{{run_id}}/evidence/{{f}}">
  {% else %}<div><a href="/run/{{run_id}}/evidence/{{f}}">{{f}} (accessibility/DOM snapshot)</a></div>{% endif %}
{% endfor %}
{% endif %}

<h2>Step-by-step record ({{events|length}} entries)</h2>
<div>
{% for e in events %}
  <div class="ev"><span class="evname">{{e.event}}</span>
    <span class="muted k">{{e.ts[11:23]}}</span>
    <span class="muted">{% for k,v in e.items() %}{% if k not in ('ts','event','seq','prev') %}{{k}}={{v}}  {% endif %}{% endfor %}</span>
  </div>
{% endfor %}
</div>
"""


def main() -> None:  # pragma: no cover - convenience runner
    import argparse

    parser = argparse.ArgumentParser(description="serve the trust dashboard")
    parser.add_argument("--port", type=int, default=8200)
    parser.add_argument("--gen-dir", default="capabilities/generated")
    parser.add_argument("--runs-dir", default="runs")
    args = parser.parse_args()
    create_dashboard(args.gen_dir, args.runs_dir).run(port=args.port)


if __name__ == "__main__":  # pragma: no cover
    main()
