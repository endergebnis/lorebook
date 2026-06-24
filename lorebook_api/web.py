"""
FastAPI SPA for Lorebook v2 — Dashboard + Setup + SSE progress.
ponytail: single file, inline HTML/JS/CSS. resolver endpoints removed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from .models import LorebookConfig
from .pipeline import LorebookPipeline
from .runner import PipelineRunner

logger = logging.getLogger("lorebook.web")

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

_runner: PipelineRunner | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _runner
    config = LorebookConfig()
    pipeline = LorebookPipeline(config)
    _runner = PipelineRunner(pipeline)
    await _runner.start()
    logger.info("Lorebook v2 API ready")
    yield
    if _runner:
        await _runner.close()
    logger.info("Lorebook v2 shut down")


app = FastAPI(title="Lorebook v2", version="2.0.0", lifespan=lifespan)


def _get_runner() -> PipelineRunner:
    if _runner is None:
        raise HTTPException(503, "Pipeline not initialized")
    return _runner


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}


@app.get("/api/stats")
async def get_stats():
    """Dashboard stats."""
    r = _get_runner()
    graph = r._pipeline.graph
    try:
        entities = await graph.get_all_entities()
        entity_count = len(entities)

        # Per-type counts
        type_counts: dict[str, int] = {}
        for e in entities:
            t = e.get("type", "Unknown")
            type_counts[t] = type_counts.get(t, 0) + 1

        # Latest entities
        latest = sorted(entities, key=lambda e: e.get("name", ""))[-10:]

        return {                                                                                                       
            "entities": entity_count,
            "per_type": type_counts,
            "latest_entities": [
                {"id": e["id"], "name": e["name"], "type": e["type"]}
                for e in latest
            ],
            "is_running": r._running,
            "is_paused": not r._pause_event.is_set(),
            "tools_enabled": r._tools_loaded,
        }
    except Exception:
        return {"entities": 0, "per_type": {}, "latest_entities": [], "is_running": False, "is_paused": False}


@app.get("/api/workers")
async def get_workers():
    """Worker endpoint list + health check."""
    r = _get_runner()
    cfg = r._pipeline.config
    urls = cfg.llm_base_urls or [cfg.llm_base_url]
    workers = []
    for i, url in enumerate(urls):
        base = url.rstrip("/")
        # Health via /health endpoint (strip /v1 if present)
        health_url = base.replace("/v1", "") + "/health"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(health_url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    online = resp.status == 200
        except Exception:
            online = False
        workers.append({"id": i, "url": base, "online": online})
    return {"workers": workers, "configured_for_pipeline": bool(cfg.llm_base_urls)}


@app.get("/api/config")
async def get_config():
    r = _get_runner()
    cfg = r._pipeline.config
    return {
        "model_name": cfg.model_name,
        "temperature": cfg.temperature,
        "concurrency": cfg.concurrency,
        "chunk_size_tokens": cfg.chunk_size_tokens,
        "llm_base_url": cfg.llm_base_url,
        "llm_base_urls": cfg.llm_base_urls,
        "neo4j_uri": cfg.neo4j_uri,
        "input_dir": cfg.input_dir,
        "tools_enabled": r._tools_loaded,
    }


@app.post("/api/config")
async def update_config(request: Request):
    """Update runtime config fields."""
    r = _get_runner()
    body = await request.json()
    cfg = r._pipeline.config

    for field in ("model_name", "temperature", "concurrency", "chunk_size_tokens"):
        if field in body:
            setattr(cfg, field, body[field])

    # Multi-instance URLs (workers pick up on next pipeline run)
    if "llm_base_urls" in body and isinstance(body["llm_base_urls"], list):
        cfg.llm_base_urls = body["llm_base_urls"]
    if "llm_base_url" in body and isinstance(body["llm_base_url"], str):
        cfg.llm_base_url = body["llm_base_url"]

    # Propagate model name to LLM client
    if "model_name" in body:
        r._pipeline.llm.model_name = body["model_name"]

    return {"status": "ok", "config": await get_config()}


@app.get("/api/models")
async def list_models():
    """List models from llama.cpp /v1/models API."""
    import aiohttp
    r = _get_runner()
    base = r._pipeline.config.llm_base_url.rstrip("/")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/models") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {"models": [m["id"] for m in data.get("data", [])]}
    except Exception:
        pass
    return {"models": []}


# -- Pipeline control --


@app.post("/api/pipeline/run")
async def pipeline_run():
    """Start full pipeline in background."""
    r = _get_runner()
    if r.is_running:
        raise HTTPException(409, "Pipeline already running")

    cfg = r._pipeline.config
    asyncio.create_task(_run_pipeline(r, cfg.input_dir))
    return {"status": "started"}


async def _run_pipeline(runner: PipelineRunner, input_dir: str):
    try:
        await runner.run_full_pipeline(input_dir)
    except Exception as e:
        logger.exception("Pipeline error")


@app.post("/api/pipeline/pause")
async def pipeline_pause():
    _get_runner().pause()
    return {"status": "paused"}


@app.post("/api/pipeline/resume")
async def pipeline_resume():
    _get_runner().resume()
    return {"status": "running"}


@app.post("/api/setup/wipe")
async def wipe_db():
    """Delete all nodes from Neo4j."""
    r = _get_runner()
    driver = r._pipeline.graph.driver
    async with driver.session() as session:
        await session.run("MATCH (n) DETACH DELETE n")
    return {"status": "wiped"}


# -- SSE progress stream --


@app.get("/api/progress/stream")
async def progress_stream():
    """SSE endpoint for real-time pipeline progress."""
    r = _get_runner()

    async def event_stream():
        queue = r.progress
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=30)
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                if msg.get("type") in ("done", "error"):
                    break
            except asyncio.TimeoutError:
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/generate")
async def generate_lorebook(entity_id: str | None = Query(default=None)):
    r = _get_runner()
    output = await r._pipeline.generate_lorebook(entity_id)
    return output.model_dump()


@app.get("/api/entities")
async def list_entities():
    r = _get_runner()
    entities = await r._pipeline.graph.get_all_entities()
    return {"entities": entities, "count": len(entities)}


@app.post("/api/export")
async def export_items():
    """Export entities as Minecraft items."""
    from .export import export_entities
    r = _get_runner()
    entities = await r._pipeline.graph.get_all_entities()
    items = export_entities(entities)
    return {"items": items, "count": len(items)}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index():
    return _HTML


_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Lorebook v2</title>
<style>
:root {
  --bg: #0f1117; --panel: #1a1d27; --border: #2a2d3a;
  --text: #e1e4eb; --dim: #6b7280; --accent: #7c3aed;
  --green: #22c55e; --yellow: #eab308; --red: #ef4444;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: system-ui, sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; }
header { background: var(--panel); border-bottom: 1px solid var(--border); padding: 12px 20px; display: flex; justify-content: space-between; align-items: center; }
header h1 { font-size: 18px; }
header .badge { font-size: 12px; padding: 3px 10px; border-radius: 12px; background: var(--accent); }
.badge.green { background: var(--green); color: #000; }
.badge.yellow { background: var(--yellow); color: #000; }
main { max-width: 1100px; margin: 0 auto; padding: 20px; display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
.panel h2 { font-size: 15px; margin-bottom: 12px; color: var(--dim); text-transform: uppercase; letter-spacing: 1px; }
.panel.wide { grid-column: 1 / -1; }
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(100px, 1fr)); gap: 10px; }
.stat { text-align: center; padding: 10px; background: var(--bg); border-radius: 6px; }
.stat .num { font-size: 28px; font-weight: 700; color: var(--accent); }
.stat .label { font-size: 11px; color: var(--dim); margin-top: 4px; }
.btn-row { display: flex; gap: 8px; flex-wrap: wrap; }
.btn { padding: 8px 16px; border: none; border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: 600; }
.btn.primary { background: var(--accent); color: #fff; }
.btn.danger { background: var(--red); color: #fff; }
.btn.warn { background: var(--yellow); color: #000; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
#log { background: var(--bg); border-radius: 6px; padding: 12px; max-height: 400px; overflow-y: auto; font-size: 13px; font-family: monospace; white-space: pre-wrap; }
#log .line { padding: 2px 0; border-bottom: 1px solid var(--border); }
#log .chunk { color: var(--accent); }
#log .phase { color: var(--yellow); font-weight: bold; }
#log .done { color: var(--green); font-weight: bold; }
#log .llm-call { color: #a78bfa; }
#log .llm-result { color: #60a5fa; }
#log .llm-out { color: var(--green); }
select, input { background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 6px 10px; border-radius: 6px; font-size: 13px; width: 100%; margin-bottom: 8px; }
label { font-size: 12px; color: var(--dim); display: block; margin-bottom: 2px; }
.progress-bar { height: 6px; background: var(--border); border-radius: 3px; margin-top: 8px; overflow: hidden; }
.progress-bar .fill { height: 100%; background: var(--accent); transition: width 0.3s; }
#status-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }
#status-dot.idle { background: var(--dim); }
#status-dot.running { background: var(--green); animation: pulse 1s infinite; }
#status-dot.paused { background: var(--yellow); }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
.eta { font-size: 12px; color: var(--dim); margin-top: 4px; }
.worker-panel { margin-top: 12px; }
.worker-row { display: flex; align-items: center; gap: 8px; padding: 4px 0; font-size: 13px; }
.worker-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.worker-dot.online { background: var(--green); }
.worker-dot.offline { background: var(--red); }
.worker-url { color: var(--dim); font-family: monospace; font-size: 11px; }
</style>
</head>
<body>

<header>
  <h1>📖 Lorebook <span style="color:var(--accent);">v2</span></h1>
  <div style="display:flex;align-items:center;gap:12px;">
    <span id="tool-badge" class="badge" style="background:#6b7280;">⚙ tools</span>
    <span id="status-dot" class="idle"></span>
    <span id="status-text" style="font-size:13px;">Idle</span>
  </div>
</header>

<main>
  <!-- Stats -->
  <div class="panel wide">
    <h2>📊 Dashboard</h2>
    <div class="stat-grid" id="stats-grid">
      <div class="stat"><div class="num" id="stat-entities">—</div><div class="label">Entities</div></div>
      <div class="stat"><div class="num" id="stat-chars">—</div><div class="label">Characters</div></div>
      <div class="stat"><div class="num" id="stat-locs">—</div><div class="label">Locations</div></div>
      <div class="stat"><div class="num" id="stat-items">—</div><div class="label">Items</div></div>
      <div class="stat"><div class="num" id="stat-concepts">—</div><div class="label">Concepts</div></div>
      <div class="stat"><div class="num" id="stat-factions">—</div><div class="label">Factions</div></div>
    </div>
    <div class="progress-bar"><div class="fill" id="global-progress" style="width:0%"></div></div>
    <div class="eta" id="eta-line"></div>
  </div>

  <!-- Controls -->
  <div class="panel">
    <h2>🎮 Pipeline</h2>
    <div class="btn-row">
      <button class="btn primary" id="btn-run" onclick="runPipeline()">▶ Run Full Pipeline</button>
      <button class="btn warn" id="btn-pause" onclick="pausePipeline()" disabled>⏸ Pause</button>
      <button class="btn primary" id="btn-resume" onclick="resumePipeline()" disabled style="display:none;">▶ Resume</button>
      <button class="btn danger" id="btn-wipe" onclick="wipeDB()">🗑 Wipe DB</button>
    </div>
  </div>

  <!-- Setup -->
  <div class="panel">
    <h2>⚙ Setup</h2>
    <label>Model</label>
    <select id="model-select" onchange="updateConfig()"></select>
    <label>Temperature</label>
    <input type="range" id="temp-slider" min="0" max="1" step="0.05" value="0.25" oninput="document.getElementById('temp-val').textContent=this.value;updateConfig()">
    <span id="temp-val" style="font-size:12px;">0.25</span>
    <label>Concurrency</label>
    <select id="concurrency-select" onchange="updateConfig()">
      <option value="1">1 (single)</option>
      <option value="2">2</option>
      <option value="4">4</option>
    </select>
    <label>Worker URLs (comma-separated)</label>
    <input type="text" id="worker-urls" placeholder="http://localhost:8080/v1, http://192.168.188.204:8080/v1" onchange="updateConfig()">
    <div id="config-saved" style="font-size:11px;color:var(--green);margin-top:4px;display:none;">✅ Saved</div>
  </div>

  <!-- Workers -->
  <div class="panel">
    <h2>🖥 Workers</h2>
    <div id="workers-container" style="font-size:13px;color:var(--dim);">Checking…</div>
  </div>

  <!-- Live log -->
  <div class="panel wide">
    <h2>📋 Progress</h2>
    <div id="log"><span style="color:var(--dim);">Waiting for pipeline start...</span></div>
  </div>

  <!-- Latest entities -->
  <div class="panel wide">
    <h2>🆕 Latest Entities</h2>
    <div id="latest-entities" style="font-size:13px;color:var(--dim);">—</div>
  </div>
</main>

<script>
const API = '';

async function loadStats() {
  try {
    const r = await fetch(API + '/api/stats');
    const d = await r.json();
    document.getElementById('stat-entities').textContent = d.entities;
    document.getElementById('stat-chars').textContent = (d.per_type || {}).Character || 0;
    document.getElementById('stat-locs').textContent = (d.per_type || {}).Location || 0;
    document.getElementById('stat-items').textContent = (d.per_type || {}).Item || 0;
    document.getElementById('stat-concepts').textContent = (d.per_type || {}).Concept || 0;
    document.getElementById('stat-factions').textContent = (d.per_type || {}).Faction || 0;

    // Latest entities
    const latest = d.latest_entities || [];
    document.getElementById('latest-entities').innerHTML = latest.length
      ? latest.map(e => `<span style="display:inline-block;margin:2px 6px;padding:2px 8px;background:var(--bg);border-radius:4px;font-size:12px;"><b>${e.name}</b> <span style="color:var(--dim);">${e.type}</span></span>`).join('')
      : '<span style="color:var(--dim);">No entities yet</span>';

    // Status dot
    const dot = document.getElementById('status-dot');
    const txt = document.getElementById('status-text');
    dot.className = d.is_paused ? 'paused' : d.is_running ? 'running' : 'idle';
    txt.textContent = d.is_paused ? 'Paused' : d.is_running ? 'Running' : 'Idle';

    // Buttons
    document.getElementById('btn-run').disabled = d.is_running;
    document.getElementById('btn-pause').style.display = d.is_running && !d.is_paused ? '' : 'none';
    document.getElementById('btn-pause').disabled = !d.is_running || d.is_paused;
    document.getElementById('btn-resume').style.display = d.is_paused ? '' : 'none';
    document.getElementById('btn-resume').disabled = !d.is_paused;
  } catch(e) { console.error(e); }
}

async function loadConfig() {
  try {
    const r = await fetch(API + '/api/config');
    const d = await r.json();
    document.getElementById('temp-slider').value = d.temperature;
    document.getElementById('temp-val').textContent = d.temperature;
    document.getElementById('concurrency-select').value = d.concurrency;
    document.getElementById('tool-badge').style.background = d.tools_enabled ? '#22c55e' : '#6b7280';
    document.getElementById('tool-badge').textContent = d.tools_enabled ? '🔧 tools on' : '⚙ tools off';
    // Worker URLs
    const urls = d.llm_base_urls || [];
    document.getElementById('worker-urls').value = urls.join(', ');
  } catch(e) { console.error(e); }
}

async function loadModels() {
  try {
    const r = await fetch(API + '/api/models');
    const d = await r.json();
    const sel = document.getElementById('model-select');
    sel.innerHTML = d.models.map(m => `<option value="${m}">${m}</option>`).join('');
    // Select current
    const cfg = await fetch(API + '/api/config').then(r => r.json());
    if (cfg.model_name) {
      const opt = [...sel.options].find(o => o.value === cfg.model_name);
      if (opt) opt.selected = true;
    }
  } catch(e) { console.error(e); }
}

async function updateConfig() {
  const model = document.getElementById('model-select').value;
  const temp = parseFloat(document.getElementById('temp-slider').value);
  const concurrency = parseInt(document.getElementById('concurrency-select').value);
  const urlsRaw = document.getElementById('worker-urls').value;
  const llm_base_urls = urlsRaw ? urlsRaw.split(',').map(s => s.trim()).filter(s => s) : [];

  await fetch(API + '/api/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({model_name: model, temperature: temp, concurrency: concurrency, llm_base_urls: llm_base_urls})
  });

  const saved = document.getElementById('config-saved');
  saved.style.display = 'block';
  setTimeout(() => saved.style.display = 'none', 2000);
}

async function runPipeline() {
  document.getElementById('btn-run').disabled = true;
  document.getElementById('log').innerHTML = '';
  logLine('phase', '🚀 Starting pipeline...');
  try {
    await fetch(API + '/api/pipeline/run', {method: 'POST'});
    startSSE();
  } catch(e) {
    logLine('', '❌ Failed to start: ' + e.message);
  }
}

async function pausePipeline() {
  await fetch(API + '/api/pipeline/pause', {method: 'POST'});
  loadStats();
}

async function resumePipeline() {
  await fetch(API + '/api/pipeline/resume', {method: 'POST'});
  loadStats();
}

async function wipeDB() {
  if (!confirm('Really delete ALL entities from Neo4j?')) return;
  await fetch(API + '/api/setup/wipe', {method: 'POST'});
  loadStats();
}

function logLine(cls, msg) {
  const log = document.getElementById('log');
  log.innerHTML += `<div class="line ${cls}">${msg}</div>`;
  log.scrollTop = log.scrollHeight;
}

let _sse = null;
function startSSE() {
  if (_sse) _sse.close();
  _sse = new EventSource(API + '/api/progress/stream');
  _sse.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'heartbeat') return;

    if (msg.type === 'status') {
      loadStats();
      if (msg.message) logLine('phase', msg.message);
    } else if (msg.type === 'phase') {
      const workers = msg.workers ? ` ${msg.workers} worker(s)` : '';
      logLine('phase', `📦 Phase: ${msg.phase}${msg.chunks ? ' (' + msg.chunks + ' chunks)' : ''}${msg.total ? ' (' + msg.total + ' chunks' + workers + ')' : ''}`);
      loadStats();
    } else if (msg.type === 'chunk') {
      const pct = ((msg.chunk_index / msg.total_chunks) * 100).toFixed(1);
      document.getElementById('global-progress').style.width = pct + '%';
      document.getElementById('eta-line').textContent = msg.eta_seconds > 0 ? `ETA: ~${Math.round(msg.eta_seconds / 60)} min` : '';
      let line = `[${msg.chunk_index}/${msg.total_chunks}] W${msg.worker ?? '?'} ${msg.volume} / ${msg.chapter} — ${msg.entities_found} entities`;
      if (msg.entity_names && msg.entity_names.length) {
        line += ` (${msg.entity_names.join(', ')})`;
      }
      logLine('chunk', line);
      loadStats();
    } else if (msg.type === 'chunk_error') {
      logLine('', `❌ [${msg.chunk_index}/${msg.total_chunks}] ${msg.volume}/${msg.chapter} FAILED`);
      loadStats();
    } else if (msg.type === 'llm_tool_call') {
      logLine('llm-call', `  🔧 Turn ${msg.turn}: LLM → ${msg.tools.join(', ')}`);
    } else if (msg.type === 'llm_tool_result') {
      const found = msg.found > 0 ? `found ${msg.found}: ${msg.names.join(', ')}` : 'no matches';
      logLine('llm-result', `  📡 ${msg.tool}: ${found}`);
    } else if (msg.type === 'llm_output') {
      logLine('llm-out', `  ✨ Turn ${msg.turn}: ${msg.entities.length} entities, ${msg.events} events`);
      if (msg.entities.length) logLine('llm-out', `     → ${msg.entities.join(', ')}`);
    } else if (msg.type === 'llm_retry') {
      logLine('', `  ⚠ Turn ${msg.turn}: ${msg.reason}, retrying...`);
    } else if (msg.type === 'done') {
      const doneMsg = msg.message || `✅ Done! ${msg.entities} entities, ${msg.events} events in ${Math.round(msg.elapsed_seconds / 60)} min`;
      logLine('done', doneMsg);
      document.getElementById('global-progress').style.width = '100%';
      document.getElementById('eta-line').textContent = '';
      _sse.close();
      loadStats();
    } else if (msg.type === 'error') {
      logLine('', '❌ Pipeline failed');
      _sse.close();
      loadStats();
    }
  };
  _sse.onerror = () => { _sse.close(); loadStats(); };
}

// Init
async function loadWorkers() {
  try {
    const r = await fetch(API + '/api/workers');
    const d = await r.json();
    const w = d.workers || [];
    document.getElementById('workers-container').innerHTML = w.length
      ? w.map(wk => `<div class="worker-row">
          <span class="worker-dot ${wk.online ? 'online' : 'offline'}"></span>
          <span>W${wk.id}</span>
          <span class="worker-url">${wk.url}</span>
          <span style="font-size:11px;">${wk.online ? '🟢 online' : '🔴 offline'}</span>
        </div>`).join('')
      : '<span style="color:var(--dim);">No workers configured</span>';
  } catch(e) { console.error(e); }
}

loadStats();
loadConfig();
loadModels();
loadWorkers();
setInterval(loadStats, 5000);
setInterval(loadWorkers, 8000);
</script>
</body>
</html>"""
