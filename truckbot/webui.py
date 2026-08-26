"""Tiny local web UI (stdlib only) so staff can run the bots without the
command line: manage the container list, choose the variables (towers,
transaction type), start/stop bots, and watch live progress.

    python -m truckbot ui            ->  http://localhost:8123

Concurrent N4 logins are allowed, so the fastest setup is ONE BOT PER
TOWER, each attached to its own debug Chrome (ports come from config
debug_ports: 109->9222, 202->9223, 203->9224, 205->9225). The page can
start/stop each tower's bot independently, or run a single rotating
session on one Chrome instead.

Attach mode still applies: the operator must have each tower's debug
Chrome open and logged in to N4 first (instructions on the page).
"""

import collections
import csv
import json
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import VALID_TOWERS, __version__
from .config import Config
from .containers import ErrorCapture, ResultsStore, group_by_tower, load_containers
from .engine import Engine
from .notify import Notifier
from .reportparse import append_to_list, parse_report

log = logging.getLogger("truckbot")


class Runner:
    """One engine on one Chrome session, in its own thread."""

    def __init__(self, key: str, cfg: Config, mode: str, tower: str | None,
                 transaction: str | None, debug_url: str,
                 results: ResultsStore, errors: ErrorCapture, on_event):
        self.key = key
        self.cfg = cfg
        self.mode = mode
        self.tower = tower
        self.transaction = transaction
        self.debug_url = debug_url
        self.results = results
        self.errors = errors
        self.on_event = on_event
        self.stop_event = threading.Event()
        self.state = "starting"
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name=f"runner-{key}")
        self.thread.start()

    def alive(self) -> bool:
        return self.thread.is_alive()

    def _emit(self, kind, **data):
        self.on_event(kind, bot=self.key, **data)

    def _run(self):
        # Playwright sync API must be created inside this thread.
        from .session import N4Session
        session = N4Session(self.cfg, debug_url=self.debug_url)
        try:
            # attach, or auto-launch Chrome + log in when credentials saved
            self._emit("connecting", detail=self.debug_url)
            session.attach_or_launch()
        except Exception as e:
            self.state = "fatal"
            self._emit("fatal", detail=(
                f"Could not attach to Chrome at {self.debug_url}: {e}. "
                f"Save your N4 login under Settings for auto-start, or "
                f"start that Chrome in debug mode and log in by hand."))
            return
        try:
            self.state = "running"
            engine = Engine(self.cfg, session, results=self.results,
                            errors=self.errors, on_event=self._emit,
                            stop_event=self.stop_event)
            engine.run(mode=self.mode, tower=self.tower,
                       transaction_type=self.transaction)
            self.state = "stopped"
        except Exception as e:
            log.exception("engine %s crashed", self.key)
            self.state = "fatal"
            self._emit("fatal", detail=str(e))
        finally:
            session.close()

    def stop(self):
        self.stop_event.set()


class Controller:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.events = collections.deque(maxlen=500)
        self.runners: dict[str, Runner] = {}
        self.results = ResultsStore(cfg.results_file)
        self.errors = ErrorCapture(cfg.errors_file)
        self.notifier = Notifier(cfg)
        self._lock = threading.Lock()

    def on_event(self, kind, **data):
        self.events.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "kind": kind, **data,
        })
        self.notifier(kind, **data)

    # --- lifecycle -----------------------------------------------------------
    def start(self, towers: list[str], parallel: bool,
              transaction: str | None) -> str | None:
        towers = [t for t in towers if t in VALID_TOWERS]
        transaction = (transaction or "").strip() or None
        with self._lock:
            self._reap()
            if parallel:
                if not towers:
                    return "tick at least one tower"
                started = []
                for t in towers:
                    if t in self.runners and self.runners[t].alive():
                        continue    # already running
                    self.runners[t] = Runner(
                        t, self.cfg, "single", t, transaction,
                        self.cfg.debug_url_for(t),
                        self.results, self.errors, self.on_event)
                    started.append(t)
                if not started:
                    return "those towers are already running"
                return None
            # one rotating session for the whole list
            if "all" in self.runners and self.runners["all"].alive():
                return "already running"
            self.runners["all"] = Runner(
                "all", self.cfg, "all", None, transaction,
                self.cfg.debug_url, self.results, self.errors, self.on_event)
            return None

    def stop(self, key: str | None = None):
        with self._lock:
            targets = [self.runners[key]] if key in self.runners \
                else list(self.runners.values())
        for r in targets:
            r.stop()
        self.on_event("stopping", bot=key or "all bots")

    def _reap(self):
        for k in [k for k, r in self.runners.items()
                  if not r.alive() and r.state in ("stopped", "fatal")]:
            self.runners.pop(k)

    # --- data for the page ---------------------------------------------------
    def status(self) -> dict:
        results = ResultsStore(self.cfg.results_file)
        pending = load_containers(self.cfg.containers_file, results.done_set())
        rows = results.rows()
        bots = {}
        for k, r in self.runners.items():
            bots[k] = {"state": r.state if r.alive() or r.state == "fatal"
                       else "stopped",
                       "alive": r.alive(),
                       "tower": r.tower, "mode": r.mode,
                       "transaction": r.transaction or "as set in N4",
                       "debug_url": r.debug_url}
        return {
            "version": __version__,
            "bots": bots,
            "any_running": any(r.alive() for r in self.runners.values()),
            "towers": list(VALID_TOWERS),
            "debug_ports": {t: self.cfg.debug_ports.get(t)
                            for t in VALID_TOWERS},
            "transaction_types": self.cfg.transaction_types,
            "pending_by_tower": {
                t: len(cs) for t, cs in
                group_by_tower(pending, self.cfg.tower_order).items()},
            "pending_total": len(pending),
            "summary": results.summary(),
            "results": rows[-30:][::-1],
            "events": list(self.events)[-80:][::-1],
        }

    # --- list management -------------------------------------------------------
    def set_containers(self, csv_text: str, mode: str = "replace") -> dict:
        """Load a pasted list ('CONTAINER,TOWER' per line; header optional).
        Order is kept as pasted - list order is FIFO booking order."""
        rows = []
        for line in csv_text.strip().splitlines():
            parts = [p.strip() for p in line.replace(";", ",").split(",")]
            if not parts or not parts[0]:
                continue
            c = parts[0].upper()
            if c == "CONTAINER":     # header
                continue
            t = parts[1] if len(parts) > 1 else ""
            rows.append((c, t))
        path = Path(self.cfg.containers_file)
        existing = []
        if mode == "append" and path.exists():
            with open(path, newline="") as f:
                existing = [(r["container"], r.get("tower", ""))
                            for r in csv.DictReader(f)]
        have = {c for c, _ in existing}
        merged = existing + [(c, t) for c, t in rows if c not in have]
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["container", "tower"])
            w.writerows(merged)
        return {"loaded": len(merged), "added": len(merged) - len(existing)}

    def get_settings(self) -> dict:
        return {"username": self.cfg.username,
                "has_password": bool(self.cfg.password),
                "auto_launch": self.cfg.auto_launch}

    def save_settings(self, username: str, password: str) -> dict:
        """Store N4 credentials in the LOCAL config.json (gitignored).
        An empty password keeps the existing one."""
        self.cfg.username = username.strip()
        if password:
            self.cfg.password = password
        path = Path("config.json")
        raw = json.loads(path.read_text()) if path.exists() else {}
        raw["username"] = self.cfg.username
        raw["password"] = self.cfg.password
        path.write_text(json.dumps(raw, indent=2))
        return self.get_settings()

    def list_rows(self) -> list[dict]:
        done = ResultsStore(self.cfg.results_file).done_set()
        return load_containers(self.cfg.containers_file, done)

    def add_container(self, container: str, tower: str) -> dict:
        container = container.strip().upper()
        if not container:
            return {"error": "container is empty"}
        if tower not in VALID_TOWERS:
            return {"error": f"tower must be one of {sorted(VALID_TOWERS)}"}
        path = Path(self.cfg.containers_file)
        rows = []
        if path.exists():
            with open(path, newline="") as f:
                rows = [(r["container"], r.get("tower", ""))
                        for r in csv.DictReader(f)]
        if any(c == container for c, _ in rows):
            return {"error": f"{container} is already on the list"}
        rows.append((container, tower))     # FIFO: new arrivals at the end
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["container", "tower"])
            w.writerows(rows)
        return {"ok": True}

    def remove_from_list(self, container: str) -> dict:
        from .containers import remove_container
        ok = remove_container(self.cfg.containers_file, container)
        return {"ok": ok}

    def make_list(self, report_text: str, tower: str) -> dict:
        if tower not in VALID_TOWERS:
            return {"error": f"tower must be one of {sorted(VALID_TOWERS)}"}
        bookable, excluded = parse_report(report_text)
        added = append_to_list(bookable, tower, self.cfg.containers_file)
        return {"bookable": len(bookable), "added": added,
                "excluded": [{"container": c, "why": w} for c, w in excluded]}


# ---------------------------------------------------------------- HTTP layer
class Handler(BaseHTTPRequestHandler):
    controller: Controller = None   # set by serve()

    def log_message(self, *a):      # quiet the default access log
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        if self.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/status":
            self._json(self.controller.status())
        elif self.path == "/api/containers":
            path = Path(self.controller.cfg.containers_file)
            text = path.read_text() if path.exists() else "container,tower\n"
            self._json({"csv": text, "rows": self.controller.list_rows()})
        elif self.path == "/api/settings":
            self._json(self.controller.get_settings())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        c = self.controller
        data = self._read_body()
        if self.path == "/api/start":
            err = c.start(data.get("towers", []),
                          bool(data.get("parallel", True)),
                          data.get("transaction"))
            self._json({"error": err} if err else {"ok": True})
        elif self.path == "/api/stop":
            c.stop(data.get("bot"))
            self._json({"ok": True})
        elif self.path == "/api/containers":
            self._json(c.set_containers(data.get("csv", ""),
                                        data.get("mode", "replace")))
        elif self.path == "/api/settings":
            self._json(c.save_settings(data.get("username", ""),
                                       data.get("password", "")))
        elif self.path == "/api/containers/add":
            self._json(c.add_container(data.get("container", ""),
                                       str(data.get("tower", ""))))
        elif self.path == "/api/containers/remove":
            self._json(c.remove_from_list(data.get("container", "")))
        elif self.path == "/api/makelist":
            self._json(c.make_list(data.get("report", ""),
                                   str(data.get("tower", ""))))
        else:
            self._json({"error": "not found"}, 404)


def serve(cfg: Config, port: int = 8123, open_browser: bool = True):
    Handler.controller = Controller(cfg)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    log.info("Web UI on http://localhost:%d  (Ctrl+C to quit)", port)
    print(f"\n  Truck Booking Bot UI ->  http://localhost:{port}\n")
    if open_browser:    # the server socket is already bound at this point
        try:
            import webbrowser
            webbrowser.open(f"http://localhost:{port}")
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        Handler.controller.stop()
        httpd.shutdown()


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Truck Booking Bot</title>
<style>
  :root { --bg:#f5f6f8; --card:#fff; --ink:#1b2430; --mut:#68727f;
          --ok:#0a7d33; --warn:#b25e00; --bad:#b3261e; --acc:#1a56c8; }
  * { box-sizing:border-box; }
  body { margin:0; font:15px/1.45 system-ui,Segoe UI,Arial,sans-serif;
         background:var(--bg); color:var(--ink); }
  header { background:#12325e; color:#fff; padding:14px 22px;
           display:flex; align-items:center; gap:14px; }
  header h1 { font-size:18px; margin:0; font-weight:600; }
  header .dot { width:12px; height:12px; border-radius:50%; background:#888; }
  header .dot.on { background:#3ddc73; box-shadow:0 0 8px #3ddc73; }
  main { max-width:1120px; margin:18px auto; padding:0 16px;
         display:grid; grid-template-columns:360px 1fr; gap:16px; }
  .card { background:var(--card); border-radius:10px; padding:16px 18px;
          box-shadow:0 1px 3px rgba(0,0,0,.08); }
  .card h2 { font-size:14px; margin:0 0 10px; text-transform:uppercase;
             letter-spacing:.04em; color:var(--mut); }
  button { font:inherit; border:0; border-radius:8px; padding:9px 16px;
           cursor:pointer; }
  .primary { background:var(--acc); color:#fff; }
  .danger  { background:var(--bad); color:#fff; }
  .ghost   { background:#e8ebf0; }
  .mini    { padding:3px 10px; font-size:12.5px; border-radius:6px; }
  button:disabled { opacity:.45; cursor:default; }
  label { display:block; margin:8px 0 3px; color:var(--mut); font-size:13px; }
  select,textarea,input[type=text],input[type=password] { width:100%; font:inherit;
      border:1px solid #cdd4dd; border-radius:7px; padding:7px; }
  textarea { font-family:ui-monospace,Consolas,monospace; font-size:13px; }
  table { border-collapse:collapse; width:100%; font-size:13.5px; }
  th,td { text-align:left; padding:4px 8px; border-bottom:1px solid #edf0f4; }
  th { color:var(--mut); font-weight:600; }
  .BOOKED { color:var(--ok); font-weight:700; }
  .SKIPPED { color:var(--warn); }
  .pill { display:inline-block; background:#e8ebf0; border-radius:20px;
          padding:2px 11px; margin:2px 4px 2px 0; font-size:13px; }
  .pill b { color:var(--acc); }
  .towerbox { display:flex; align-items:center; gap:8px; padding:7px 10px;
              border:1px solid #dde2e9; border-radius:8px; margin:6px 0; }
  .towerbox .st { margin-left:auto; font-size:12.5px; color:var(--mut); }
  .towerbox .st.running { color:var(--ok); font-weight:700; }
  .towerbox .st.fatal { color:var(--bad); font-weight:700; }
  #log { height:290px; overflow-y:auto; font:12.5px ui-monospace,Consolas,monospace;
         background:#10161f; color:#c7d3e0; border-radius:8px; padding:10px; }
  #log .booked { color:#5df08d; font-weight:700; }
  #log .skipped { color:#f0b429; }
  #log .fatal,#log .error { color:#ff7b6e; }
  .help { font-size:13px; color:var(--mut); }
  .help code { background:#eef1f5; padding:1px 5px; border-radius:4px;
               word-break:break-all; }
  .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  #msg { min-height:18px; font-size:13px; color:var(--bad); margin-top:6px; }
  details { margin-top:10px; }
  summary { cursor:pointer; color:var(--acc); font-size:13.5px; }
</style></head><body>
<header>
  <div class="dot" id="dot"></div>
  <h1>Truck Booking Bot &mdash; ICTSI Durban (AVEMEL LOG)</h1>
  <span id="stat" style="opacity:.85;font-size:13.5px">idle</span>
</header>
<main>
  <section>
    <div class="card">
      <h2>N4 login (auto-start)</h2>
      <div class="row">
        <div style="flex:1"><label>Username</label>
          <input type="text" id="user" placeholder="TRK-..."></div>
        <div style="flex:1"><label>Password</label>
          <input type="password" id="pass" placeholder="unchanged"></div>
      </div>
      <div class="row" style="margin-top:8px">
        <button class="ghost" id="saveCreds">Save login</button>
        <span class="help" id="credState"></span>
      </div>
      <div class="help" style="margin-top:6px">Stays in
        <code>config.json</code> on this PC only. With a saved login the
        bot opens Chrome and logs in to N4 by itself when you press
        Start.</div>
    </div>
    <div class="card" style="margin-top:16px">
      <h2>Bots</h2>
      <label>Transaction type</label>
      <select id="txn">
        <option value="">Leave as set in N4 (safest)</option>
      </select>
      <label>Mode</label>
      <select id="mode">
        <option value="parallel">One bot per tower (parallel &mdash; fastest)</option>
        <option value="one">One session rotating all towers</option>
      </select>
      <div id="towers"></div>
      <div class="row" style="margin-top:12px">
        <button class="primary" id="start">&#9654; Start</button>
        <button class="danger" id="stopall">&#9632; Stop all</button>
      </div>
      <div id="msg"></div>
      <details>
        <summary>Before you press Start (per tower)</summary>
        <div class="help">
          <p>Each tower's bot attaches to its OWN Chrome. Start one debug
          Chrome per tower you want to run (ports next to the tickboxes):</p>
          <p><code>"C:\Program Files\Google\Chrome\Application\chrome.exe"
             --remote-debugging-port=9222
             --user-data-dir="C:\navis-chrome-109"</code></p>
          <p>(or just double-click the <b>start_chrome_&lt;tower&gt;.bat</b>
          files in the bot folder)</p>
          <p>In each Chrome: log in to N4, click <b>+</b> to open
          <b>Add Appointment</b>, and hand-set <b>Trucking Company =
          AVEMEL LOG</b> (and the Transaction Type, if you chose "leave as
          set"). Then press Start here.</p>
        </div>
      </details>
    </div>
    <div class="card" style="margin-top:16px">
      <h2>Container list (top = booked first)</h2>
      <div id="pending"></div>
      <div style="max-height:220px;overflow-y:auto;margin:8px 0">
        <table><thead><tr><th>#</th><th>Container</th><th>Tower</th>
          <th></th></tr></thead><tbody id="listRows"></tbody></table>
      </div>
      <div class="row">
        <input type="text" id="newCont" placeholder="Container e.g. PCIU9529335"
               style="flex:1;text-transform:uppercase">
        <select id="newTower" style="width:80px">
          <option>109</option><option>202</option>
          <option>203</option><option>205</option>
        </select>
        <button class="ghost" id="addCont">Add</button>
      </div>
      <label>Paste list (<code>CONTAINER,TOWER</code> per line, FIFO order)</label>
      <textarea id="csv" rows="7" placeholder="PCIU9529335,109&#10;MSDU4523340,203"></textarea>
      <div class="row" style="margin-top:8px">
        <button class="ghost" id="replace">Replace list</button>
        <button class="ghost" id="append">Add to list</button>
        <button class="ghost" id="show">Show current</button>
      </div>
      <details>
        <summary>Import from an N4 report instead</summary>
        <label>Tower for these containers</label>
        <select id="mlTower">
          <option>109</option><option>202</option>
          <option>203</option><option>205</option>
        </select>
        <label>Paste the whole "Units assigned" report text</label>
        <textarea id="report" rows="6"></textarea>
        <button class="ghost" style="margin-top:8px" id="makelist">
          Filter &amp; add bookable (oldest first)</button>
        <div id="mlOut" class="help"></div>
      </details>
    </div>
  </section>
  <section>
    <div class="card">
      <h2>Progress <span id="summary" style="text-transform:none"></span></h2>
      <div id="log"></div>
    </div>
    <div class="card" style="margin-top:16px">
      <h2>Latest results</h2>
      <table><thead><tr><th>Time</th><th>Container</th><th>Status</th>
        <th>Detail</th></tr></thead><tbody id="results"></tbody></table>
    </div>
  </section>
</main>
<script>
const $ = id => document.getElementById(id);
const api = (p, body) => fetch(p, body ? {method:'POST',
  headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}
  : undefined).then(r => r.json());
const esc = s => String(s ?? '').replace(/[&<>]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

let towersInit = false;

function renderTowers(s) {
  const bots = s.bots || {};
  if (!towersInit) {
    $('towers').innerHTML = s.towers.map(t => `
      <div class="towerbox">
        <input type="checkbox" id="tw${t}" checked>
        <label for="tw${t}" style="margin:0">Tower ${t}
          <span class="help">(port ${s.debug_ports[t] ?? 9222})</span></label>
        <span class="st" id="st${t}"></span>
        <button class="mini danger" id="stop${t}" style="display:none">stop</button>
      </div>`).join('');
    s.towers.forEach(t =>
      $('stop' + t).onclick = () => api('/api/stop', {bot: t}).then(refresh));
    const sel = $('txn');
    (s.transaction_types || []).forEach(x => {
      const o = document.createElement('option');
      o.value = x; o.textContent = x; sel.appendChild(o);
    });
    towersInit = true;
  }
  s.towers.forEach(t => {
    const b = bots[t];
    const el = $('st' + t);
    el.textContent = b ? b.state : '';
    el.className = 'st ' + (b ? b.state : '');
    $('stop' + t).style.display = (b && b.alive) ? '' : 'none';
  });
}

async function loadSettings() {
  const st = await api('/api/settings');
  $('user').value = st.username || '';
  $('credState').textContent = st.has_password
    ? 'login saved - auto-start is on' : 'no login saved (attach by hand)';
}
$('saveCreds').onclick = async () => {
  await api('/api/settings',
    {username: $('user').value, password: $('pass').value});
  $('pass').value = '';
  loadSettings();
};
loadSettings();

async function refreshList() {
  const r = await api('/api/containers');
  $('listRows').innerHTML = (r.rows || []).map((x, i) =>
    `<tr><td>${i + 1}</td><td>${esc(x.container)}</td>` +
    `<td>${esc(x.tower)}</td><td><button class="mini ghost" ` +
    `onclick="removeCont('${esc(x.container)}')">remove</button></td></tr>`
  ).join('') || '<tr><td colspan="4" class="help">List is empty.</td></tr>';
}
window.removeCont = async c => {
  if (!confirm(`Remove ${c} from the list?`)) return;
  await api('/api/containers/remove', {container: c});
  refreshList(); refresh();
};
$('addCont').onclick = async () => {
  const r = await api('/api/containers/add',
    {container: $('newCont').value, tower: $('newTower').value});
  if (r.error) { alert(r.error); return; }
  $('newCont').value = '';
  refreshList(); refresh();
};
refreshList();
setInterval(refreshList, 5000);

$('start').onclick = async () => {
  $('msg').textContent = '';
  let towers = [];
  document.querySelectorAll('#towers input:checked')
    .forEach(cb => towers.push(cb.id.slice(2)));
  const r = await api('/api/start', {
    towers, parallel: $('mode').value === 'parallel',
    transaction: $('txn').value });
  if (r.error) $('msg').textContent = r.error;
  refresh();
};
$('stopall').onclick = () => api('/api/stop', {}).then(refresh);

$('replace').onclick = () => loadList('replace');
$('append').onclick  = () => loadList('append');
async function loadList(mode) {
  const r = await api('/api/containers', {csv: $('csv').value, mode});
  alert(`List now has ${r.loaded} container(s) (${r.added} added).`);
  refresh();
}
$('show').onclick = async () => {
  const r = await api('/api/containers');
  $('csv').value = r.csv.replace(/^container,tower\n/, '');
};
$('makelist').onclick = async () => {
  const r = await api('/api/makelist',
    {report: $('report').value, tower: $('mlTower').value});
  if (r.error) { $('mlOut').textContent = r.error; return; }
  $('mlOut').innerHTML = `Added <b>${r.added.length}</b> of ${r.bookable} ` +
    `bookable. Excluded ${r.excluded.length}: ` +
    r.excluded.map(e => `${esc(e.container)} (${esc(e.why)})`).join(', ');
  refresh();
};

async function refresh() {
  let s;
  try { s = await api('/api/status'); } catch { return; }
  renderTowers(s);
  const running = Object.values(s.bots || {}).filter(b => b.alive);
  $('dot').className = 'dot' + (s.any_running ? ' on' : '');
  $('stat').textContent = s.any_running
    ? `running: ${running.map(b => b.tower || 'all').join(', ')}`
    : 'idle';
  $('stopall').disabled = !s.any_running;
  const sum = s.summary || {};
  $('summary').textContent =
    ` — booked ${sum.BOOKED || 0} · skipped ${sum.SKIPPED || 0}` +
    ` · pending ${s.pending_total}`;
  $('pending').innerHTML = Object.entries(s.pending_by_tower)
    .map(([t, n]) => `<span class="pill">Tower ${esc(t)}: <b>${n}</b></span>`)
    .join('') || '<span class="help">List is empty.</span>';
  $('results').innerHTML = (s.results || []).map(r =>
    `<tr><td>${esc(r.timestamp).slice(11, 19)}</td><td>${esc(r.container)}</td>` +
    `<td class="${esc(r.status)}">${esc(r.status)}</td>` +
    `<td>${esc(r.detail)}</td></tr>`).join('');
  $('log').innerHTML = (s.events || []).map(e => {
    const extra = Object.entries(e).filter(([k]) =>
      !['time', 'kind', 'bot'].includes(k))
      .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`)
      .join(' ');
    const bot = e.bot ? `(${e.bot}) ` : '';
    return `<div class="${esc(e.kind)}">[${esc(e.time)}] ${esc(bot)}` +
           `${esc(e.kind).toUpperCase()} ${esc(extra)}</div>`;
  }).join('');
}
refresh();
setInterval(refresh, 2000);
</script>
</body></html>"""
