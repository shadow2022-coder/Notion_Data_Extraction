# app.py
import os
import io
import time
import re
import json
import logging
from datetime import date, timedelta, datetime
from collections import deque
from typing import Optional

import requests
from flask import Flask, render_template_string, request, jsonify, send_file, abort
from docx import Document

# --------------------
# App configuration
# --------------------
app = Flask(__name__)
# Protect against very large bodies
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2 MB

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
APP_API_KEY = os.getenv("APP_API_KEY")  # optional: require X-API-KEY header
NOTION_VERSION = "2022-06-28"

if not NOTION_TOKEN:
    # Fail fast for deployments that forget to set secrets
    raise RuntimeError("Environment variable NOTION_TOKEN is required")

# --------------------
# Logging (no secrets)
# --------------------
logger = logging.getLogger("notion_extractor")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
logger.addHandler(handler)

# --------------------
# Security headers
# --------------------
@app.after_request
def set_security_headers(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    # Basic CSP - adapt if you add external assets
    resp.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'"
    return resp

# --------------------
# Lightweight rate limiter (per-IP)
# Note: per-instance only. For robust rate limiting, use Redis or a managed solution.
# --------------------
RATE_LIMIT_REQUESTS = 30
RATE_LIMIT_WINDOW = 60  # seconds
_client_requests = {}  # ip -> deque[timestamps]

def is_rate_limited(client_ip: str) -> bool:
    now = time.time()
    dq = _client_requests.setdefault(client_ip, deque())
    # purge old timestamps
    while dq and dq[0] <= now - RATE_LIMIT_WINDOW:
        dq.popleft()
    if len(dq) >= RATE_LIMIT_REQUESTS:
        return True
    dq.append(now)
    return False

# --------------------
# HTML (removed token input; server uses NOTION_TOKEN)
# --------------------
HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Notion Data Extractor</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{font-family:Inter,system-ui,Arial,Helvetica,sans-serif;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);padding:24px}
.container{max-width:900px;margin:0 auto;background:#fff;padding:28px;border-radius:12px}
h1{margin-bottom:6px}label{font-weight:600;display:block;margin-top:12px}input,select,button{width:100%;padding:10px;border-radius:8px;border:1px solid #e6e6e6}
button{background:#4f46e5;color:#fff;border:none;margin-top:16px;padding:12px;font-weight:700}
.results{background:#f8f9fa;padding:12px;border-radius:8px;margin-top:16px;white-space:pre-wrap}
.small{font-size:13px;color:#666}
.downloads{display:flex;gap:8px;margin-top:12px}
.downloads button{flex:1;background:#10b981;color:white;border:none;padding:10px;border-radius:8px}
</style>
</head>
<body>
<div class="container">
  <h1>Notion Data Extractor</h1>
  <p class="small">This instance uses a server-side Notion integration token stored securely as an environment variable. Do not paste tokens here.</p>

  <div class="small" style="background:#eef2ff;padding:10px;border-radius:8px;">
    <strong>Setup</strong>
    <ol>
      <li>Create a Notion Integration and copy its secret (server-side).</li>
      <li>Share your database with the integration (Notion → ••• → Connections).</li>
      <li>Provide Database ID below. The server uses the stored token to access Notion.</li>
    </ol>
  </div>

  <form id="extractForm">
    <label for="databaseId">Database ID *</label>
    <input id="databaseId" required placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"/>

    <label for="dateProperty">Date Property (optional)</label>
    <input id="dateProperty" placeholder="Date (default)"/>

    <label for="personProperty">Person Property (optional)</label>
    <input id="personProperty" placeholder="Assignee (default)"/>

    <label for="extractMode">Extract Mode</label>
    <select id="extractMode">
      <option value="all">All Data</option>
      <option value="today">Today Only</option>
      <option value="specific_date">Specific Date</option>
      <option value="date_range">Date Range</option>
      <option value="last_n_days">Last N Days</option>
    </select>

    <div id="dateControls" style="display:none">
      <label for="specificDate">Specific Date</label>
      <input id="specificDate" type="date"/>
      <label for="startDate">Start Date</label>
      <input id="startDate" type="date"/>
      <label for="endDate">End Date</label>
      <input id="endDate" type="date"/>
      <label for="lastNDays">Last N Days</label>
      <input id="lastNDays" type="number" min="1" max="365" value="7"/>
    </div>

    <button id="extractBtn" type="submit">Extract</button>
  </form>

  <div class="results" id="results" style="display:none"></div>

  <div class="downloads" id="downloads" style="display:none">
    <button onclick="download('json')">Download JSON</button>
    <button onclick="download('txt')">Download TXT</button>
    <button onclick="download('docx')">Download DOCX</button>
  </div>
</div>

<script>
const extractMode = document.getElementById("extractMode");
const dateControls = document.getElementById("dateControls");
extractMode.addEventListener("change", () => {
  if (["specific_date","date_range","last_n_days"].includes(extractMode.value)) dateControls.style.display="block";
  else dateControls.style.display="none";
});

async function postJson(path, body) {
  const headers = {"Content-Type":"application/json"};
  // If you set APP_API_KEY, include it here: headers["x-api-key"] = "<your_key>"
  const resp = await fetch(path, {method:"POST", headers, body: JSON.stringify(body)});
  const json = await resp.json().catch(()=>({error:"invalid json"}));
  if (!resp.ok) throw new Error(json.error || "Request failed");
  return json;
}

let lastData = null;

document.getElementById("extractForm").addEventListener("submit", async (e)=>{
  e.preventDefault();
  const db = document.getElementById("databaseId").value.trim();
  if (!db) { alert("Database ID required"); return; }
  const body = {
    database_id: db,
    date_property: document.getElementById("dateProperty").value.trim() || "Date",
    person_property: document.getElementById("personProperty").value.trim() || "Assignee",
    extract_mode: document.getElementById("extractMode").value,
    specific_date: document.getElementById("specificDate").value,
    start_date: document.getElementById("startDate").value,
    end_date: document.getElementById("endDate").value,
    last_n_days: document.getElementById("lastNDays").value
  };
  document.getElementById("results").style.display="block";
  document.getElementById("results").textContent="Extracting...";
  try {
    const res = await postJson("/extract", body);
    lastData = res.data || [];
    if (!Array.isArray(lastData) || lastData.length===0) {
      document.getElementById("results").textContent = "No pages found.";
      document.getElementById("downloads").style.display="none";
      return;
    }
    document.getElementById("results").textContent = JSON.stringify(lastData, null, 2);
    document.getElementById("downloads").style.display="flex";
  } catch(err) {
    document.getElementById("results").textContent = "Error: " + err.message;
    document.getElementById("downloads").style.display="none";
  }
});

async function download(fmt) {
  if (!lastData) return alert("No data to download");
  try {
    const resp = await fetch("/download/" + fmt, {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({data: lastData})
    });
    if (!resp.ok) {
      const e = await resp.json().catch(()=>({error:"download failed"}));
      throw new Error(e.error || "Download failed");
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `notion_export_${new Date().toISOString().split("T")[0]}.${fmt}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch(e) {
    alert("Download error: " + e.message);
  }
}
</script>
</body>
</html>
"""

# --------------------
# Validation helpers
# --------------------
DB_ID_PATTERN = re.compile(r"^[0-9a-fA-F-]{32,64}$")
ALLOWED_MODES = {"today", "specific_date", "date_range", "last_n_days", "all"}

def validate_database_id(db_id: str) -> bool:
    return bool(DB_ID_PATTERN.match(db_id.strip()))

def safe_get_json(req):
    try:
        return req.get_json(force=True)
    except Exception:
        return {}

# --------------------
# Date filter builder
# --------------------
def get_date_filter(mode: str, date_property: str, **kwargs) -> Optional[dict]:
    today = date.today().isoformat()
    if mode == "today":
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        return {"and":[{"property":date_property,"date":{"on_or_after":today}},
                       {"property":date_property,"date":{"before":tomorrow}}]}
    if mode == "specific_date":
        specific = kwargs.get("specific_date")
        if not specific:
            return None
        next_day = (datetime.strptime(specific, "%Y-%m-%d") + timedelta(days=1)).date().isoformat()
        return {"and":[{"property":date_property,"date":{"on_or_after":specific}},
                       {"property":date_property,"date":{"before":next_day}}]}
    if mode == "date_range":
        start = kwargs.get("start_date")
        end = kwargs.get("end_date")
        if not start or not end:
            return None
        return {"and":[{"property":date_property,"date":{"on_or_after":start}},
                       {"property":date_property,"date":{"on_or_before":end}}]}
    if mode == "last_n_days":
        try:
            n = int(kwargs.get("last_n_days", 7))
        except Exception:
            n = 7
        start = (date.today() - timedelta(days=n-1)).isoformat()
        return {"and":[{"property":date_property,"date":{"on_or_after":start}},
                       {"property":date_property,"date":{"on_or_before":today}}]}
    return None

# --------------------
# Helpers: JSON/TXT/DOCX bytes
# --------------------
def _json_bytes(data):
    out = io.BytesIO()
    out.write(json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"))
    out.seek(0)
    return out

def _txt_bytes(data):
    text = f"NOTION EXPORT - {date.today().isoformat()}\n" + ("="*80) + "\n\n"
    for item in data:
        text += "\n" + ("="*80) + "\n"
        text += f"TITLE: {item.get('title','Untitled')}\n"
        text += f"DATE: {item.get('date','No date')}\n"
        text += f"BY: {item.get('assignee','Unassigned')}\n"
        text += f"URL: {item.get('url','')}\n"
        text += ("="*80) + "\n\n"
        text += item.get("content","") + "\n\n"
    b = io.BytesIO()
    b.write(text.encode("utf-8"))
    b.seek(0)
    return b

def _docx_bytes(data):
    doc = Document()
    doc.add_heading("Notion Export", level=1)
    doc.add_paragraph(f"Export date: {date.today().isoformat()}")
    for i, item in enumerate(data, start=1):
        doc.add_heading(f"{i}. {item.get('title','Untitled')}", level=2)
        doc.add_paragraph(f"Date: {item.get('date','No date')}")
        doc.add_paragraph(f"Assignee: {item.get('assignee','Unassigned')}")
        doc.add_paragraph(f"URL: {item.get('url','')}")
        doc.add_paragraph("")
        content = item.get("content","")
        if content:
            for para in content.splitlines():
                if para.strip():
                    doc.add_paragraph(para)
                else:
                    doc.add_paragraph("")
        if i != len(data):
            doc.add_page_break()
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf

# --------------------
# Routes
# --------------------
@app.route("/", methods=["GET"])
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/extract", methods=["POST"])
def extract():
    # basic per-IP rate limiting
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if is_rate_limited(client_ip):
        return jsonify({"error":"Too many requests"}), 429

    # optional API key enforcement
    if APP_API_KEY:
        key = request.headers.get("x-api-key")
        if not key or key != APP_API_KEY:
            return jsonify({"error":"Unauthorized"}), 401

    payload = safe_get_json(request)
    database_id = (payload.get("database_id") or "").strip()
    date_property = payload.get("date_property") or "Date"
    person_property = payload.get("person_property") or "Assignee"
    extract_mode = payload.get("extract_mode") or "all"

    # validate
    if not database_id or not validate_database_id(database_id):
        return jsonify({"error":"Invalid database_id"}), 400
    if extract_mode not in ALLOWED_MODES:
        return jsonify({"error":"Invalid extract_mode"}), 400

    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION
    }

    date_filter = get_date_filter(
        extract_mode, date_property,
        specific_date=payload.get("specific_date"),
        start_date=payload.get("start_date"),
        end_date=payload.get("end_date"),
        last_n_days=payload.get("last_n_days")
    )
    query_body = {"filter": date_filter} if date_filter else {}

    try:
        resp = requests.post(
            f"https://api.notion.com/v1/databases/{database_id}/query",
            headers=headers,
            json=query_body,
            timeout=25
        )
    except requests.RequestException as exc:
        logger.warning("Notion API request failed: %s", str(exc))
        return jsonify({"error":"Upstream request failed"}), 502

    if resp.status_code != 200:
        # log details server-side; do not leak raw upstream content
        try:
            logger.info("Notion returned %s: %.200s", resp.status_code, resp.text)
        except Exception:
            pass
        return jsonify({"error":"Notion API returned an error"}), resp.status_code

    try:
        pages = resp.json().get("results", [])
    except Exception:
        return jsonify({"error":"Invalid response from Notion"}), 502

    if not pages:
        return jsonify({"data": []}), 200

    processed = []
    for page in pages:
        props = page.get("properties", {})

        # Title detection
        title = "Untitled"
        for n in ("Name","Title","name","title"):
            if n in props and props[n].get("title"):
                tlist = props[n]["title"]
                if isinstance(tlist, list) and tlist:
                    title = tlist[0].get("plain_text","Untitled")
                break

        # Date
        page_date = "No date"
        if date_property in props and props[date_property].get("date"):
            page_date = props[date_property]["date"].get("start", "No date")

        # Assignee
        assignee = "Unassigned"
        if person_property in props and props[person_property].get("people"):
            ppl = props[person_property]["people"]
            if isinstance(ppl, list) and ppl:
                assignee = ppl[0].get("name") or ppl[0].get("id") or "Unknown"

        # Blocks — single page children (no deep pagination)
        content = ""
        try:
            br = requests.get(f"https://api.notion.com/v1/blocks/{page.get('id')}/children",
                              headers=headers, timeout=25)
            if br.ok:
                blocks = br.json().get("results", [])
                parts = []
                for block in blocks:
                    btype = block.get("type")
                    if btype and block.get(btype, {}).get("rich_text"):
                        parts.append(" ".join([t.get("plain_text","") for t in block[btype]["rich_text"]]))
                    elif block.get("paragraph") and block["paragraph"].get("rich_text"):
                        parts.append(" ".join([t.get("plain_text","") for t in block["paragraph"]["rich_text"]]))
                content = "\n".join([p for p in parts if p])
        except Exception:
            logger.debug("Failed to fetch blocks for page %s", page.get("id"))

        processed.append({
            "page_id": page.get("id"),
            "title": title,
            "date": page_date,
            "assignee": assignee,
            "content": content,
            "url": f"https://www.notion.so/{(page.get('id') or '').replace('-','')}"
        })

    return jsonify({"data": processed}), 200

@app.route("/download/<fmt>", methods=["POST"])
def download(fmt):
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if is_rate_limited(client_ip):
        return jsonify({"error":"Too many requests"}), 429

    # optional auth
    if APP_API_KEY:
        key = request.headers.get("x-api-key")
        if not key or key != APP_API_KEY:
            return jsonify({"error":"Unauthorized"}), 401

    payload = safe_get_json(request)
    data = payload.get("data", [])
    if not isinstance(data, list) or not data:
        return jsonify({"error":"No data provided"}), 400

    if fmt == "json":
        buf = _json_bytes(data)
        return send_file(buf, as_attachment=True, download_name=f"notion_export_{date.today().isoformat()}.json", mimetype="application/json")
    elif fmt == "txt":
        buf = _txt_bytes(data)
        return send_file(buf, as_attachment=True, download_name=f"notion_export_{date.today().isoformat()}.txt", mimetype="text/plain")
    elif fmt == "docx":
        buf = _docx_bytes(data)
        return send_file(buf, as_attachment=True, download_name=f"notion_export_{date.today().isoformat()}.docx", mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    else:
        return jsonify({"error":"Unsupported format"}), 400

# --------------------
# Local entrypoint only
# --------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "3000"))
    # Local only: do not enable debug in production
    app.run(host="0.0.0.0", port=port)
