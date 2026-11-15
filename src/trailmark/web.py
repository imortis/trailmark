"""Live demo: ask Trailmark where a change belongs, on three stores side by side.

  plain     text indexes only; what a plain (non-graph) store could answer
  graph     the code graph in HydraDB as ingested, plus git history
  learned   the same graph after it was taught the outcomes of the first N tasks

The tasks offered are real later commits of the indexed repository that none
of the three has seen. Standard library only.

Run:  python -m trailmark.web --name reef --commits data/repos/reef --since <rev>
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .bench import get_tasks
from .hydra import Hydra
from .index import Workspace
from .learn import LearnConfig, Learner
from .present import changed_together, snippet, top_symbols
from .retrieve import Config, Retriever

MAX_TEXT = 6000
MODES = ("plain", "graph", "learned")


class Demo:
    def __init__(self, args: argparse.Namespace):
        self.ws = ws = Workspace(args.name)
        self.db = Hydra()
        self.lock = threading.Lock()
        dense = (ws.dir / "dense.npy").exists()
        stamp = int(time.time()) % 10_000_000

        flat = Config(use_dense=dense, use_graph=False, use_symgraph=False, use_uses=False, kinds=(), top_k=30)
        off = dict(use_history=False, use_experience=False, use_trails=False, use_policy=False, use_heat=False)
        self.learners = {
            "plain": Learner(Retriever(ws, None, flat), self.db, LearnConfig(run=f"p{stamp}", **off)),
            "graph": Learner(Retriever(ws, self.db, Config(use_dense=dense, top_k=30)), self.db,
                             LearnConfig(run=f"g{stamp}", use_policy=False)),
            "learned": Learner(Retriever(ws, self.db, Config(use_dense=dense, top_k=30)), self.db, LearnConfig(run=f"l{stamp}")),
        }
        tasks, _ = get_tasks(args, ws)
        self.taught = min(args.teach, max(0, len(tasks) - 5))
        t0 = time.perf_counter()
        teacher = self.learners["learned"]
        for task in tasks[: self.taught]:
            teacher.record(task.text, teacher.locate(task.text), task.gold, key=task.key, ts=task.ts)
        print(f"taught the learning graph {self.taught} merged changes in {time.perf_counter() - t0:.1f}s", flush=True)
        self.tasks = [
            {"key": t.key, "title": t.text.splitlines()[0][:140], "text": t.text,
             "answer": [ws.file_by_id[g]["path"] for g in t.gold]}
            for t in tasks[self.taught :]
        ]
        summary = Path(args.summary)
        self.summary = json.loads(summary.read_text(encoding="utf-8")) if summary.exists() else {}

    def info(self) -> dict:
        learned = self.learners["learned"]
        return {
            "workspace": self.ws.name,
            "graph": self.ws.meta.get("stats", {}),
            "history_tasks": learned.history_loaded,
            "taught": self.taught,
            "trails": len(learned.trails),
            "cofix_pairs": len(learned.cofix) // 2,
            "held_out": len(self.tasks),
            "summary": self.summary,
        }

    def locate(self, text: str, k: int = 8) -> dict:
        ws, out = self.ws, {}
        with self.lock:
            for mode in MODES:
                learner = self.learners[mode]
                t0 = time.perf_counter()
                res = learner.locate(text)
                ms = (time.perf_counter() - t0) * 1000
                hits = res.hits[:k]
                symbols = top_symbols(ws, [h.file_id for h in hits], text, res.mention_symbols)
                files = []
                for i, h in enumerate(hits):
                    entry = {"path": h.path, "why": h.reasons[:4], "read_first": symbols.get(h.file_id, [])}
                    if i < 2 and entry["read_first"]:
                        entry["code"] = snippet(ws, h.path, entry["read_first"][0]["lines"])
                    files.append(entry)
                block = {"files": files, "ranking": [h.path for h in res.hits], "graph_reads": res.db_queries, "ms": round(ms, 1)}
                if mode != "plain":
                    block["identifiers"] = [
                        {"name": ident, "used_in": [ws.file_by_id[f]["path"] for f in fids[:10]], "total": len(fids)}
                        for ident, fids in res.usage[:6]
                    ]
                if mode == "learned":
                    block["changed_together"] = changed_together(ws, learner, [h.file_id for h in hits[:4]])
                out[mode] = block
        return out

    def why(self, a: str, b: str) -> dict:
        with self.lock:
            steps = self.learners["learned"].r.why(a, b)
        return {"connected": bool(steps), "path": steps or []}


class Handler(BaseHTTPRequestHandler):
    demo: Demo

    def log_message(self, *args) -> None:  # keep the console quiet
        pass

    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json")

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        try:
            if url.path == "/":
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif url.path == "/api/info":
                self._json(self.demo.info())
            elif url.path == "/api/tasks":
                self._json(self.demo.tasks)
            elif url.path == "/api/why":
                q = parse_qs(url.query)
                self._json(self.demo.why(q.get("from", [""])[0], q.get("to", [""])[0]))
            elif url.path == "/healthz":
                self._json({"status": "ok"})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # a demo should say what went wrong, not drop the connection
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self) -> None:
        try:
            if urlsplit(self.path).path != "/api/locate":
                self._json({"error": "not found"}, 404)
                return
            length = min(int(self.headers.get("Content-Length") or 0), 200_000)
            body = json.loads(self.rfile.read(length) or b"{}")
            text = str(body.get("text", "")).strip()[:MAX_TEXT]
            if not text:
                self._json({"error": "describe a change first"}, 400)
                return
            self._json(self.demo.locate(text))
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", required=True)
    p.add_argument("--repo")
    p.add_argument("--commits")
    p.add_argument("--since")
    p.add_argument("--parquet", default="data/swebench/test.parquet")
    p.add_argument("--teach", type=int, default=60, help="merged changes the learning graph is taught at startup")
    p.add_argument("--summary", default="results/summary.json")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7860)
    args = p.parse_args(argv)
    Handler.demo = Demo(args)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Trailmark demo on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trailmark · live demo</title>
<style>
:root{--bg:#f7f6f2;--card:#fff;--ink:#1c1b19;--mute:#6b675f;--line:#e2ded4;--accent:#1f6f5c;--good:#17804f;--goodbg:#e3f4ea;--warn:#a2551a;--code:#f1efe8}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--card:#1f1f1c;--ink:#ecebe6;--mute:#a09c92;--line:#33322d;--accent:#5fc7ad;--good:#6fd39a;--goodbg:#1d3528;--warn:#e0a26b;--code:#282723}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1240px;margin:0 auto;padding:24px 16px 60px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}h2{font-size:16px;margin:28px 0 10px}
.lede{color:var(--mute);max-width:70ch;margin:0 0 18px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0 6px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.stat b{display:block;font-size:22px;font-variant-numeric:tabular-nums}.stat span{color:var(--mute);font-size:12.5px}
.row{display:grid;grid-template-columns:minmax(0,320px) minmax(0,1fr);gap:14px;align-items:start}
@media (max-width:820px){.row{grid-template-columns:1fr}}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px}
.tasks{max-height:330px;overflow:auto;margin:0;padding:0;list-style:none}
.tasks li{padding:7px 8px;border-radius:7px;cursor:pointer;font-size:13.5px;border:1px solid transparent}
.tasks li:hover{background:var(--code)}.tasks li.on{border-color:var(--accent)}
textarea{width:100%;min-height:150px;resize:vertical;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);padding:10px;font:13.5px/1.45 ui-monospace,Consolas,monospace}
button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:9px 16px;font:600 14px system-ui;cursor:pointer}
@media (prefers-color-scheme:dark){button{color:#10201b}}button:disabled{opacity:.5;cursor:wait}
.answer{margin:10px 0 0;font-size:13.5px;color:var(--mute)}.answer code{color:var(--good)}
.cols{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:14px}
@media (max-width:980px){.cols{grid-template-columns:1fr}}
.col h3{margin:0 0 2px;font-size:15px}.col .sub{color:var(--mute);font-size:12.5px;margin-bottom:8px}
.verdict{font-size:13px;margin:6px 0 10px;padding:6px 8px;border-radius:7px;background:var(--code)}
.verdict.all{background:var(--goodbg);color:var(--good)}
ol.files{margin:0;padding:0;list-style:none;counter-reset:f}
ol.files li{counter-increment:f;border-top:1px solid var(--line);padding:7px 0;font-size:13px}
ol.files li::before{content:counter(f) ".";color:var(--mute);margin-right:6px;font-variant-numeric:tabular-nums}
.path{font-family:ui-monospace,Consolas,monospace;font-size:12.5px;word-break:break-all}
li.hit .path{color:var(--good);font-weight:600}li.hit{background:linear-gradient(90deg,var(--goodbg),transparent)}
.why{color:var(--mute);font-size:12px;margin:2px 0 0 20px}
details{margin-top:8px;font-size:12.5px}summary{cursor:pointer;color:var(--accent)}
pre{background:var(--code);padding:8px;border-radius:7px;overflow:auto;font-size:11.5px;max-height:220px}
.tag{display:inline-block;background:var(--code);border-radius:5px;padding:1px 6px;margin:2px 3px 0 0;font:11.5px ui-monospace,Consolas,monospace}
table{border-collapse:collapse;font-size:13.5px;width:100%}th,td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left}th{color:var(--mute);font-weight:600}
.note{color:var(--mute);font-size:12.5px;margin-top:6px}.err{color:var(--warn)}a{color:var(--accent)}
</style></head><body><main>
<h1>Trailmark</h1>
<p class="lede">A context layer for coding agents on HydraDB. Describe a change, and Trailmark walks a graph of the codebase to say which files it belongs in. Below, the same question is answered by three stores at once, on changes none of them has seen.</p>
<div class="stats" id="stats"></div>

<h2>Try it</h2>
<div class="row">
  <div class="panel"><div style="font-weight:600;margin-bottom:6px">Real changes the graph has not seen</div><ul class="tasks" id="tasks"></ul></div>
  <div class="panel">
    <textarea id="text" placeholder="Pick a change on the left, or describe your own (this repository is an agent-training platform: storage, inference, evaluation, sandboxing)."></textarea>
    <div style="display:flex;gap:12px;align-items:center;margin-top:8px;flex-wrap:wrap"><button id="go">Locate</button><span class="note" id="status"></span></div>
    <div class="answer" id="answer"></div>
  </div>
</div>
<div class="cols" id="cols"></div>

<h2>Measured results</h2>
<div class="panel" id="results"><span class="note">loading…</span></div>
<p class="note">Source, benchmarks and per-task records: <a href="https://github.com/imortis/trailmark">github.com/imortis/trailmark</a></p>
</main>
<script>
const $=s=>document.querySelector(s), esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const LABEL={plain:["Plain store","Text indexes only. No graph."],graph:["Graph in HydraDB","Structure, usage edges and git history."],learned:["Graph after learning","The same graph, taught by merged changes."]};
let answer=[], info={};
const pct=x=>(x*100).toFixed(1)+"%", num=x=>Math.round(x).toLocaleString();
async function boot(){
  info=await (await fetch("/api/info")).json();
  const g=info.graph||{};
  const edges=Object.entries(g).filter(([k])=>k===k.toUpperCase()).reduce((a,[,v])=>a+v,0);
  $("#stats").innerHTML=[[num((g.files||0)+(g.symbols||0)+(g.names||0)),"nodes in HydraDB"],[num(edges),"edges"],[num(info.history_tasks),"past commits as experience"],[info.taught,"merged changes taught"],[num(info.cofix_pairs),"file pairs learned to change together"],[info.held_out,"unseen changes to try"]].map(([b,s])=>`<div class="stat"><b>${b}</b><span>${s}</span></div>`).join("");
  LABEL.learned[1]=`The same graph, taught by ${info.taught} merged changes.`;
  const tasks=await (await fetch("/api/tasks")).json();
  $("#tasks").innerHTML=tasks.map((t,i)=>`<li data-i="${i}">${esc(t.title)}</li>`).join("");
  $("#tasks").onclick=e=>{const li=e.target.closest("li");if(!li)return;document.querySelectorAll(".tasks li").forEach(x=>x.classList.remove("on"));li.classList.add("on");const t=tasks[li.dataset.i];$("#text").value=t.text;answer=t.answer;run()};
  $("#text").oninput=()=>{answer=[];$("#answer").innerHTML=""};
  $("#go").onclick=run; results();
}
async function run(){
  const text=$("#text").value.trim(); if(!text)return;
  $("#go").disabled=true;$("#status").textContent="walking the graph…";
  try{
    const r=await (await fetch("/api/locate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text})})).json();
    if(r.error) throw new Error(r.error);
    $("#answer").innerHTML=answer.length?`Files the real change touched: ${answer.map(a=>`<code>${esc(a)}</code>`).join(", ")}`:"";
    $("#cols").innerHTML=Object.keys(LABEL).map(m=>col(m,r[m])).join("");
    $("#status").textContent="";
  }catch(e){$("#status").innerHTML=`<span class="err">${esc(e.message)}</span>`}
  $("#go").disabled=false;
}
function col(mode,b){
  const top=b.files.map(f=>f.path), ranks=answer.map(a=>b.ranking.indexOf(a)+1);
  let verdict="";
  if(answer.length){const found=ranks.filter(r=>r>0&&r<=5).length, all=found===answer.length;
    verdict=`<div class="verdict ${all?"all":""}">${found} of ${answer.length} answer file${answer.length>1?"s":""} in the top 5`+(all?"":` · ranks ${ranks.map(r=>r||"–").join(", ")}`)+`</div>`}
  const files=b.files.map(f=>`<li class="${answer.includes(f.path)?"hit":""}"><span class="path">${esc(f.path)}</span>`+
    (f.why.length?`<div class="why">${f.why.map(esc).join(" · ")}</div>`:"")+
    (f.read_first.length?`<div class="why">read: ${f.read_first.map(s=>`${esc(s.symbol)} (${s.lines})`).join(", ")}</div>`:"")+
    (f.code?`<details><summary>code</summary><pre>${esc(f.code)}</pre></details>`:"")+`</li>`).join("");
  let extra="";
  if(b.identifiers&&b.identifiers.length) extra+=`<details open><summary>Who uses the names in this task (USES edges)</summary>${b.identifiers.map(i=>`<div style="margin-top:5px"><b>${esc(i.name)}</b> · ${i.total} file${i.total>1?"s":""}<br>${i.used_in.map(p=>`<span class="tag">${esc(p)}</span>`).join("")}</div>`).join("")}</details>`;
  if(b.changed_together&&b.changed_together.length) extra+=`<details open><summary>Changed together in past merges (COFIX edges)</summary>${b.changed_together.map(c=>`<div style="margin-top:5px"><span class="path">${esc(c.file)}</span><br>${c.with.map(w=>`<span class="tag">${esc(w.path)} ×${w.times}</span>`).join("")}</div>`).join("")}</details>`;
  return `<div class="panel col"><h3>${LABEL[mode][0]}</h3><div class="sub">${LABEL[mode][1]} ${b.graph_reads} HydraDB reads, ${b.ms} ms.</div>${verdict}<ol class="files">${files}</ol>${extra}</div>`;
}
function results(){
  const s=info.summary||{}; if(!s.agent){$("#results").innerHTML='<span class="note">Results summary not bundled; see the repository README.</span>';return}
  const a=s.agent, rows=[["Search tools only",a.baseline],["Trailmark tool, graph switched off",a.flat],["Trailmark on HydraDB",a.graph],["Trailmark on HydraDB, learning",a.learning]].filter(r=>r[1]);
  let h=`<b>An agent (${esc(a.model)}) naming the files a change needs, ${a.tasks} tasks, unseen repository</b><table><tr><th></th><th>All files found</th><th>Tokens / task</th><th>Turns / task</th></tr>`+
    rows.map(([n,r])=>`<tr><td>${n}</td><td>${pct(r.all_found)}</td><td>${num(r.tokens)}</td><td>${r.turns.toFixed(1)}</td></tr>`).join("")+`</table>`;
  if(s.learning_curve){const c=s.learning_curve;h+=`<p style="margin:16px 0 6px"><b>Does it keep improving?</b> ${esc(c.title||"All files found, by position in the task stream")}</p><table><tr><th></th>${c.buckets.map(b=>`<th>${esc(b)}</th>`).join("")}</tr>`+c.rows.map(r=>`<tr><td>${esc(r.name)}</td>${r.values.map(v=>`<td>${pct(v)}</td>`).join("")}</tr>`).join("")+`</table>`}
  $("#results").innerHTML=h+`<p class="note">${esc(s.note||"")} Learning table: each issue is scored at its own date; the learning graph has absorbed every commit merged before it, the frozen graph has not.</p>`;
}
boot();
</script></body></html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
