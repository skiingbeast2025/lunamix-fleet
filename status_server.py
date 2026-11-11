#!/usr/bin/env python3
"""
==============================================================================
HBU2 24/7 Service - Micro Status Server & Web Dashboard (status_server.py)
==============================================================================
Zero-dependency embedded HTTP server providing:
  - GET /            : Responsive dark-mode live telemetry dashboard
  - GET /api/status  : Machine-readable JSON metrics
  - POST /api/pause  : Toggle or set pause/resume state
==============================================================================
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "logs", "telemetry_state.json")

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>HBU2 24/7 Simulator Dashboard</title>
  <style>
    :root {
      --bg: #0b0f19;
      --card-bg: #111827;
      --border: #1f2937;
      --text: #f3f4f6;
      --text-muted: #9ca3af;
      --green: #10b981;
      --yellow: #f59e0b;
      --red: #ef4444;
      --blue: #3b82f6;
      --cyan: #06b6d4;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace; }
    body { background-color: var(--bg); color: var(--text); padding: 24px; min-height: 100vh; }
    .container { max-width: 1100px; margin: 0 auto; }
    header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }
    h1 { font-size: 1.5rem; display: flex; align-items: center; gap: 10px; }
    .badge { padding: 4px 12px; border-radius: 9999px; font-size: 0.85rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; }
    .badge-running { background: rgba(16, 185, 129, 0.2); color: var(--green); border: 1px solid var(--green); }
    .badge-paused { background: rgba(245, 158, 11, 0.2); color: var(--yellow); border: 1px solid var(--yellow); }
    .badge-stopped { background: rgba(239, 68, 68, 0.2); color: var(--red); border: 1px solid var(--red); }
    .controls { display: flex; gap: 12px; align-items: center; }
    button { background: var(--blue); color: white; border: none; padding: 8px 16px; border-radius: 6px; font-weight: 600; cursor: pointer; transition: opacity 0.15s; font-size: 0.9rem; }
    button:hover { opacity: 0.85; }
    button.btn-pause { background: var(--yellow); color: #000; }
    button.btn-resume { background: var(--green); color: #000; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
    .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 18px; }
    .card-title { color: var(--text-muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; }
    .card-value { font-size: 1.8rem; font-weight: 700; }
    .card-sub { font-size: 0.75rem; color: var(--text-muted); margin-top: 4px; }
    .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    @media (max-width: 768px) { .two-col { grid-template-columns: 1fr; } }
    .panel { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 18px; max-height: 440px; display: flex; flex-direction: column; }
    .panel-header { font-size: 1rem; font-weight: 700; margin-bottom: 12px; display: flex; justify-content: space-between; }
    .panel-body { overflow-y: auto; flex: 1; }
    table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
    th { text-align: left; padding: 8px; color: var(--text-muted); border-bottom: 1px solid var(--border); font-weight: 600; }
    td { padding: 8px; border-bottom: 1px solid rgba(255, 255, 255, 0.05); }
    .log-item { padding: 8px 0; border-bottom: 1px solid rgba(255, 255, 255, 0.05); font-size: 0.82rem; }
    .log-meta { color: var(--cyan); font-weight: 600; margin-right: 6px; }
    .log-bot { color: var(--green); font-weight: 600; margin-right: 6px; }
    .log-text { color: var(--text); }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1>HBU2 24/7 Simulator <span id="status-badge" class="badge badge-stopped">Checking...</span></h1>
        <div style="font-size: 0.8rem; color: var(--text-muted); margin-top: 4px;">Anti-Mute Maximum Speed (5.1s staggered pacing)</div>
      </div>
      <div class="controls">
        <button id="toggle-btn" class="btn-pause" onclick="togglePause()">Pause Service</button>
      </div>
    </header>

    <div class="grid">
      <div class="card">
        <div class="card-title">Total Messages Sent</div>
        <div id="stat-total" class="card-value">0</div>
        <div id="stat-rate" class="card-sub">0.0 msgs/min</div>
      </div>
      <div class="card">
        <div class="card-title">Active Bots Online</div>
        <div id="stat-bots" class="card-value">0</div>
        <div id="stat-pool" class="card-sub">Loaded from CSV</div>
      </div>
      <div class="card">
        <div class="card-title">Success Rate</div>
        <div id="stat-success" class="card-value" style="color: var(--green);">100%</div>
        <div id="stat-errors" class="card-sub">0 errors | 0 mutes</div>
      </div>
      <div class="card">
        <div class="card-title">Service Uptime</div>
        <div id="stat-uptime" class="card-value" style="font-size: 1.4rem;">00:00:00</div>
        <div id="stat-latency" class="card-sub">Ping: 0ms</div>
      </div>
    </div>

    <div class="two-col">
      <div class="panel">
        <div class="panel-header">Active Bots Fleet (<span id="bot-count-label">0</span>)</div>
        <div class="panel-body">
          <table>
            <thead>
              <tr>
                <th>Bot Name</th>
                <th>Sent</th>
                <th>Last Active</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody id="bots-tbody">
              <tr><td colspan="4" style="color: var(--text-muted);">No active bots</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <div class="panel-header">Recent Chat Feed</div>
        <div id="logs-container" class="panel-body">
          <div style="color: var(--text-muted);">Waiting for messages...</div>
        </div>
      </div>
    </div>
  </div>

  <script>
    async function updateDashboard() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();

        // Status Badge
        const badge = document.getElementById('status-badge');
        const btn = document.getElementById('toggle-btn');
        if (data.status === 'RUNNING') {
          badge.className = 'badge badge-running';
          badge.textContent = '● RUNNING';
          btn.className = 'btn-pause';
          btn.textContent = 'Pause Service';
        } else if (data.status === 'PAUSED') {
          badge.className = 'badge badge-paused';
          badge.textContent = '⏸ PAUSED';
          btn.className = 'btn-resume';
          btn.textContent = 'Resume Service';
        } else {
          badge.className = 'badge badge-stopped';
          badge.textContent = '○ STOPPED';
          btn.style.display = 'none';
        }

        // Stats
        document.getElementById('stat-total').textContent = (data.messages_sent || 0).toLocaleString();
        document.getElementById('stat-rate').textContent = (data.messages_per_minute || 0).toFixed(1) + ' msgs/min';
        document.getElementById('stat-bots').textContent = data.active_bots || 0;
        document.getElementById('stat-pool').textContent = 'Pool: ' + (data.total_bots_configured || 'ALL');
        document.getElementById('stat-uptime').textContent = data.uptime_str || '00:00:00';
        document.getElementById('stat-latency').textContent = 'Server Ping: ' + (data.last_latency_ms || 0) + 'ms';

        const totalAttempts = (data.messages_sent || 0) + (data.messages_failed || 0);
        const rate = totalAttempts > 0 ? ((data.messages_sent / totalAttempts) * 100).toFixed(1) : '100.0';
        document.getElementById('stat-success').textContent = rate + '%';
        document.getElementById('stat-errors').textContent = (data.messages_failed || 0) + ' fails | ' + (data.banned_count || 0) + ' mutes';

        // Bots Table
        document.getElementById('bot-count-label').textContent = (data.bot_states || []).length;
        const tbody = document.getElementById('bots-tbody');
        if (data.bot_states && data.bot_states.length > 0) {
          tbody.innerHTML = data.bot_states.slice(0, 50).map(b => `
            <tr>
              <td style="font-weight:600; color:var(--cyan);">${b.bot}</td>
              <td>${b.sent || 0}</td>
              <td>${b.last_seen || 'just now'}</td>
              <td style="color: ${b.status === 'OK' ? 'var(--green)' : 'var(--yellow)'};">${b.status || 'OK'}</td>
            </tr>
          `).join('');
        }

        // Logs Feed
        const logsBox = document.getElementById('logs-container');
        if (data.recent_messages && data.recent_messages.length > 0) {
          logsBox.innerHTML = data.recent_messages.slice(-30).reverse().map(m => `
            <div class="log-item">
              <span class="log-meta">[${m.time || ''}]</span>
              <span class="log-bot">&lt;${m.bot || 'bot'}&gt;</span>
              <span class="log-text">"${m.msg || ''}"</span>
            </div>
          `).join('');
        }
      } catch (err) {
        console.warn('Dashboard poll error:', err);
      }
    }

    async function togglePause() {
      try {
        await fetch('/api/toggle-pause', { method: 'POST' });
        setTimeout(updateDashboard, 200);
      } catch (err) {
        alert('Could not update pause state: ' + err);
      }
    }

    setInterval(updateDashboard, 2000);
    updateDashboard();
  </script>
</body>
</html>
"""

class TelemetryHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Silence default access logs to stdout
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode("utf-8"))
        elif parsed.path == "/api/status":
            state = get_live_state()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(state).encode("utf-8"))
        elif parsed.path == "/api/toggle-pause":
            state = toggle_service_pause()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(state).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/api/toggle-pause", "/api/pause"):
            state = toggle_service_pause()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(state).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

def get_live_state():
    """Reads telemetry state from disk or returns default empty state."""
    default_state = {
        "status": "STOPPED",
        "uptime_seconds": 0,
        "uptime_str": "00:00:00",
        "active_bots": 0,
        "total_bots_configured": "ALL",
        "messages_sent": 0,
        "messages_failed": 0,
        "messages_per_minute": 0.0,
        "banned_count": 0,
        "last_latency_ms": 0,
        "paused": False,
        "bot_states": [],
        "recent_messages": []
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                default_state.update(data)
        except Exception:
            pass
    return default_state

def toggle_service_pause():
    """Toggles paused flag in state and signal files."""
    state = get_live_state()
    new_paused = not state.get("paused", False)
    state["paused"] = new_paused
    state["status"] = "PAUSED" if new_paused else "RUNNING"
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass

    # Touch pause trigger file for daemon
    pause_trigger = os.path.join(BASE_DIR, "logs", ".pause_toggle")
    try:
        with open(pause_trigger, "w") as f:
            f.write("1" if new_paused else "0")
    except Exception:
        pass

    return state

def start_status_server(port=8088):
    """Starts the embedded HTTP status server in a daemon thread."""
    try:
        server = HTTPServer(("0.0.0.0", port), TelemetryHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server
    except Exception as e:
        print(f"[!] Warning: Could not start web status server on port {port}: {e}")
        return None

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Standalone Status Server Dashboard")
    parser.add_argument("--port", type=int, default=8088, help="Port to listen on (default: 8088)")
    args = parser.parse_args()
    print(f"Starting status server on http://localhost:{args.port}...")
    server = start_status_server(args.port)
    if server:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            server.shutdown()
