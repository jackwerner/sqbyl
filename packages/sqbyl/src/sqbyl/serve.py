"""`sqbyl serve` / `sqbyl run <release>` — a local chat endpoint (spec §9.2, plan 9.2).

Two thin wrappers over the same stateless pipeline:

* **`serve`** points at the working project (dev): ask questions against the brain
  you're building, iterate, and leave 👍/👎 that become synth/eval candidates.
* **`run <release>`** points at a shipped release JSON with an injected DB + model —
  the same thing `sqbyl_runtime.load()` gives a production app, but with a local UI.

**Intentionally not hardened (spec §9.2).** This is a localhost developer convenience,
not a production server: no auth, no pooling, no multi-tenancy, no TLS. It binds to
`127.0.0.1` by default and prints a loud warning if you bind it anywhere else — those
concerns are the host's job, and this must not go on the open internet.

Built on the stdlib `http.server` so the runtime/toolkit take on **no web dependency**.
Requests are served one at a time (a lock around each `ask`), which keeps the shared
spend meter's check-then-record honest and sidesteps single-file-DB threading — fine
for a single developer at a keyboard. Every question is a **paid** call: a per-call
estimate is shown up front, each call meters to `.sqbyl/usage.db`, and an optional
session `--budget` hard-stops new questions once reached (invariant 5).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from sqbyl.models.feedback import FeedbackRecord
from sqbyl_runtime.cost import SpendMeter, price_usage
from sqbyl_runtime.models import Dialect
from sqbyl_runtime.pipeline import AgentResult
from sqbyl_runtime.state.layout import SqbylPaths
from sqbyl_runtime.state.usage import UsageRecord, UsageStore

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass
class Endpoint:
    """What the HTTP layer needs, independent of project-vs-release: how to answer, what
    model answers (for pricing), how to close, and some display metadata."""

    label: str
    model: str
    dialect: Dialect
    ask_one: Callable[[str], AgentResult]
    close: Callable[[], None]
    source: str  # "serve" or "run"


class ChatServer:
    """A localhost chat server over one :class:`Endpoint`. Serialize + meter every ask."""

    def __init__(
        self,
        endpoint: Endpoint,
        *,
        paths: SqbylPaths,
        budget: float | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.paths = paths.ensure()
        self._lock = threading.Lock()
        # The meter tracks the session budget in memory (thread-safe); each call is persisted
        # to a *fresh* UsageStore in the request thread (store=None here) — SQLite connections
        # can't cross threads, and ThreadingHTTPServer hands each request to a new one.
        self._meter = SpendMeter(budget=budget, store=None, command=endpoint.source)

    @property
    def spent(self) -> float:
        return self._meter.spent

    @property
    def budget(self) -> float | None:
        return self._meter.budget

    def meta(self) -> dict[str, Any]:
        """Session facts the page shows *before* any spend: the per-call estimate, the
        session cap, and what's spent so far — so the dollar consent is legible in the
        browser (where the clicking happens), not only in the launch terminal."""
        return {
            "model": self.endpoint.model,
            "label": self.endpoint.label,
            "per_call_estimate_usd": price_usage_estimate(self.endpoint.model),
            "budget": self._meter.budget,
            "spent_usd": self._meter.spent,
        }

    def ask(self, question: str) -> dict[str, Any]:
        """Answer one question (serialized), meter it, and return a JSON-able dict.

        Returns ``{"budget_exhausted": true, ...}`` without spending when the session
        budget is already reached — the hard cap on an interactive session.
        """
        with self._lock:
            est = price_usage_estimate(self.endpoint.model)
            if self._meter.would_exceed(est):
                return {
                    "budget_exhausted": True,
                    "spent_usd": self._meter.spent,
                    "budget": self._meter.budget,
                    "error": (
                        f"session budget ${self._meter.budget:.2f} reached "
                        f"(${self._meter.spent:.4f} spent) — restart with a higher --budget"
                    ),
                }
            result = self.endpoint.ask_one(question)
            cost = self._meter.record(
                result.usage,
                model=self.endpoint.model,
                role="agent",
                run_id=result.trace_id,
            )
            # Persist to usage.db in *this* (request) thread — a fresh connection each time.
            with UsageStore(self.paths.usage_db) as store:
                store.record(
                    UsageRecord.from_usage(
                        result.usage,
                        model=self.endpoint.model,
                        command=self.endpoint.source,
                        role="agent",
                        cost_usd=cost,
                        run_id=result.trace_id,
                    )
                )
        return _result_to_json(
            result, cost=cost, spent=self._meter.spent, budget=self._meter.budget
        )

    def feedback(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Append a 👍/👎 to the feedback log (an eval/synth candidate, spec §7)."""
        record = FeedbackRecord(
            trace_id=str(payload.get("trace_id", "")),
            question=str(payload.get("question", "")),
            sql=str(payload.get("sql", "")),
            rating="up" if str(payload.get("rating")) == "up" else "down",
            ok=bool(payload.get("ok", False)),
            note=(str(payload["note"]) if payload.get("note") else None),
            source=self.endpoint.source,
        )
        with self._lock, self.paths.feedback_log.open("a") as fh:
            fh.write(record.model_dump_json() + "\n")
        return {"stored": True}

    def close(self) -> None:
        self.endpoint.close()


# --- the stdlib HTTP glue --------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = "sqbyl-serve"

    @property
    def _chat(self) -> ChatServer:
        chat = self.server._chat  # type: ignore[attr-defined]
        assert isinstance(chat, ChatServer)
        return chat

    def log_message(self, *_args: object) -> None:  # quiet by default
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        if self.path in ("/", "/index.html"):
            self._send(200, _PAGE.encode(), content_type="text/html; charset=utf-8")
        elif self.path == "/meta":
            self._send_json(self._chat.meta())
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
        body = self._read_json()
        if body is None:
            self._send(400, b'{"error":"invalid JSON body"}')
            return
        if self.path == "/ask":
            question = str(body.get("question", "")).strip()
            if not question:
                self._send(400, b'{"error":"missing question"}')
                return
            self._send_json(self._chat.ask(question))
        elif self.path == "/feedback":
            self._send_json(self._chat.feedback(body))
        else:
            self._send(404, b'{"error":"not found"}')

    # -- helpers --

    def _read_json(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        try:
            parsed = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def _send_json(self, payload: dict[str, Any]) -> None:
        self._send(200, json.dumps(payload).encode())

    def _send(self, code: int, body: bytes, *, content_type: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _ChatHTTPServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer that carries its :class:`ChatServer`."""

    def __init__(self, address: tuple[str, int], chat: ChatServer) -> None:
        super().__init__(address, _Handler)
        self._chat = chat


def make_server(chat: ChatServer, *, host: str = "127.0.0.1", port: int = 8765) -> _ChatHTTPServer:
    """Build (but don't start) the HTTP server bound to ``host:port``."""
    return _ChatHTTPServer((host, port), chat)


def is_local_host(host: str) -> bool:
    return host in _LOCAL_HOSTS


# --- building the endpoint from a project or a release ---------------------------


def project_endpoint(
    project: object,
    *,
    llm: object | None = None,
    trace_writer: object | None = None,
) -> Endpoint:
    """An :class:`Endpoint` over the working project (the `serve` source)."""
    from sqbyl.llm import build_llm_client
    from sqbyl.project import Project
    from sqbyl.projectfiles import load_knowledge
    from sqbyl_runtime.llm.base import LLMClient
    from sqbyl_runtime.pipeline import ask as run_ask
    from sqbyl_runtime.state.traces import TraceWriter

    assert isinstance(project, Project)
    knowledge = load_knowledge(project)
    model = project.manifest.model.for_role("agent")
    selection_model = project.manifest.model.for_role("selection")
    repairs = project.manifest.defaults.self_repair_attempts
    client = llm if isinstance(llm, LLMClient) else build_llm_client(project.manifest)
    paths = SqbylPaths(project.root).ensure()
    writer = (
        trace_writer
        if isinstance(trace_writer, TraceWriter)
        else TraceWriter(paths.traces_dir / "serve.jsonl")
    )
    db = project.connect()

    def _ask(question: str) -> AgentResult:
        return run_ask(
            question,
            knowledge=knowledge,
            db=db,
            llm=client,
            model=model,
            selection_model=selection_model,
            self_repair_attempts=repairs,
            trace_writer=writer,
        )

    return Endpoint(
        label=f"project {project.manifest.name!r}",
        model=model,
        dialect=knowledge.dialect,
        ask_one=_ask,
        close=db.close,
        source="serve",
    )


def release_endpoint(
    release_path: str | Path,
    *,
    db: str,
    model: str,
    provider: str = "anthropic",
    project_root: str | Path,
    llm: object | None = None,
) -> Endpoint:
    """An :class:`Endpoint` over a shipped release JSON (the `run` source).

    Uses the production loader (`sqbyl_runtime.load()`), so the served behavior is
    exactly what an embedding app gets — same schema/model mismatch warnings and all.
    """
    from sqbyl_runtime.llm.base import LLMClient
    from sqbyl_runtime.runtime import load
    from sqbyl_runtime.state.traces import TraceWriter

    paths = SqbylPaths(Path(project_root)).ensure()
    agent = load(
        release_path,
        db=db,
        model=model,
        provider=provider,
        llm=llm if isinstance(llm, LLMClient) else None,
        trace_writer=TraceWriter(paths.traces_dir / "run.jsonl"),
    )
    return Endpoint(
        label=f"release {Path(release_path).name}",
        model=model,
        dialect=agent.db.dialect,
        ask_one=agent.ask,
        close=agent.close,
        source="run",
    )


# --- pricing + serialization -----------------------------------------------------

# A rough per-question price used only to *gate* the next question against the session
# budget before spending — the real cost is metered after the call (invariant 5). Kept
# conservative (a whole answer's worth of tokens) so the cap never silently overshoots.
_EST_INPUT, _EST_OUTPUT = 1800, 350


def price_usage_estimate(model: str) -> float:
    from sqbyl_runtime.llm.base import Usage

    return price_usage(Usage(input_tokens=_EST_INPUT, output_tokens=_EST_OUTPUT), model)


def _result_to_json(
    result: AgentResult, *, cost: float, spent: float, budget: float | None
) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "plan": result.plan,
        "sql": result.sql,
        "columns": result.columns,
        # Rows are the answer the user asked for, returned to their own localhost browser.
        # They are NOT persisted anywhere (feedback keeps only question+SQL, spec §13).
        "rows": [[_jsonable(v) for v in row] for row in result.rows[:200]],
        "row_count": len(result.rows),
        "used_assets": result.used_assets,
        "selected_tables": result.selected_tables,
        "selection_strategy": result.selection_strategy,
        "selection_fell_back": result.selection_fell_back,
        "error": result.error,
        "trace_id": result.trace_id,
        "tokens": result.usage.total_tokens,
        "cost_usd": cost,
        "spent_usd": spent,
        "budget": budget,
        "latency_ms": result.latency_ms,
    }


def _jsonable(value: object) -> object:
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


# The one-page chat UI. Deliberately tiny and dependency-free — a textarea, a table, and
# 👍/👎 that POST to /feedback. Not styled to impress; it's a local dev affordance.
_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>sqbyl</title>
<style>
 body{font:14px system-ui,sans-serif;max-width:820px;margin:2rem auto;padding:0 1rem;color:#111}
 textarea{width:100%;height:4rem;font:inherit;padding:.5rem}
 button{font:inherit;padding:.4rem .8rem;cursor:pointer}
 pre{background:#f5f5f5;padding:.6rem;overflow:auto;border-radius:4px}
 table{border-collapse:collapse;margin:.5rem 0}td,th{border:1px solid #ddd;padding:.3rem .5rem}
 .muted{color:#666;font-size:12px}.err{color:#b00}.fb button{margin-right:.4rem}
</style></head><body>
<h2>sqbyl <span class="muted" id="src"></span></h2>
<p class="muted">Local dev chat — not hardened, do not expose. Every question is a paid call.</p>
<textarea id="q" placeholder="Ask a question about your data…"></textarea>
<div><button onclick="ask()">Ask</button> <span class="muted" id="meter"></span></div>
<div id="out"></div>
<script>
// Show the per-call cost + session budget BEFORE the first click, so the dollar consent is
// legible to the person clicking (not only in the launch terminal).
async function loadMeta(){
 const m=await (await fetch('/meta')).json();
 document.getElementById('src').textContent=m.label+' · '+m.model;
 window._est=m.per_call_estimate_usd; window._budget=m.budget;
 setMeter(0,m.spent_usd||0,m.budget);
}
function setMeter(cost,spent,budget){
 let t='~$'+(window._est||0).toFixed(4)+'/question · $'+(spent||0).toFixed(4)+' spent';
 if(budget!=null)t+=' / $'+budget.toFixed(2)+' cap ($'+Math.max(0,budget-spent).toFixed(4)+' left)';
 document.getElementById('meter').textContent=t;
}
async function ask(){
 const q=document.getElementById('q').value.trim(); if(!q) return;
 const out=document.getElementById('out'); out.innerHTML='<p class="muted">thinking…</p>';
 const r=await fetch('/ask',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({question:q})});
 const d=await r.json(); render(q,d);
}
function render(q,d){
 const out=document.getElementById('out');
 if(d.budget_exhausted){out.innerHTML='<p class="err">'+d.error+'</p>';return;}
 let h='<h3>Plan</h3><p>'+esc(d.plan)+'</p><h3>SQL</h3><pre>'+esc(d.sql)+'</pre>';
 if(d.error){h+='<p class="err">error: '+esc(d.error)+'</p>';}
 else{h+='<h3>Rows ('+d.row_count+')</h3>'+table(d.columns,d.rows);}
 h+='<p class="muted">tables: '+(d.selected_tables||[]).join(', ')+
    (d.selection_fell_back?' · selection fell back':'')+'</p>';
 if((d.used_assets||[]).length)
   h+='<p class="muted">cited trusted assets: '+esc((d.used_assets||[]).join(', '))+'</p>';
 h+='<p class="muted">trace: '+esc(d.trace_id||'')+'</p>';
 // NOTE: trace_id is interpolated into an onclick JS-string below. It is safe only because
 // trace_id is always server-generated hex — esc() does NOT escape quotes, so never route
 // user- or LLM-controlled text into this attribute position without quote-escaping it.
 h+='<div class="fb">Was this right? '+
    '<button onclick="fb(\\''+d.trace_id+'\\',\\'up\\','+d.ok+')">👍</button>'+
    '<button onclick="fb(\\''+d.trace_id+'\\',\\'down\\','+d.ok+')">👎</button>'+
    '<span class="muted" id="fbmsg"></span></div>';
 out.innerHTML=h; window._last={q:q,sql:d.sql};
 setMeter(d.cost_usd,d.spent_usd,d.budget);
}
async function fb(trace_id,rating,ok){
 await fetch('/feedback',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({trace_id:trace_id,rating:rating,ok:ok,
     question:window._last.q,sql:window._last.sql})});
 document.getElementById('fbmsg').textContent=' — thanks, saved.';
}
function table(cols,rows){
 let h='<table><tr>';for(const c of cols)h+='<th>'+esc(c)+'</th>';h+='</tr>';
 for(const r of rows){h+='<tr>';for(const v of r)h+='<td>'+esc(String(v))+'</td>';h+='</tr>';}
 return h+'</table>';
}
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
loadMeta();
</script></body></html>"""
