# Evidence

End-to-end proof of the core loop, captured 2026-08-14 against the local
Fairview Teller fixture app. All data is fake seed data.

| file | what it shows |
|---|---|
| `artifact.json` | the capability the LLM-driven discovery produced — this exact file is what the replay runs below executed |
| `discovery-run/` | the genuine LLM-driven happy run (goal → observe/decide/act → done). `transcript.json` is the full model conversation; `trace.jsonl` includes per-call token usage |
| `discovery-run-outcome/` | the second discovery leg, run with a member ID known not to exist, ending in `report_outcome(MEMBER_NOT_FOUND)` — this is where the artifact's not-found recognizer comes from |
| `replay-run/` | deterministic replay of `artifact.json` with a **fresh** parameter (member 67890, which no discovery run ever saw) → `SUCCESS` with the typed balance |
| `replay-run-not-found/` | replay with a missing member → `BUSINESS_OUTCOME MEMBER_NOT_FOUND`, an answer, not a crash — with the matched region text as auditable evidence |
| `discovery-report.json` | run metrics: endings, step counts, LLM calls, token usage, wall-clock |

Replay involves no model: no `llm_call` events appear in either replay trace,
and the test suite proves the property hermetically (subprocess, API keys
stripped, non-loopback network blocked — `tests/test_zero_llm.py`).

Discovery model: an open-weights model served via an OpenAI-compatible
endpoint (see `src/hands/llm.py` for the configured default). Discovery cost
for both legs combined: ~6.4k prompt + ~0.6k completion tokens.
