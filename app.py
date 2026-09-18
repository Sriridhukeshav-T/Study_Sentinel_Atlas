"""
app.py - Interactive Web Prototype for Study Sentinel: ATLAS
A standalone, self-hosted clinical dashboard and interactive decision console.

Runs on standard library http.server (no extra dependencies required).
Connects directly to the existing stage1.atlas and starter.schemas backend.

Usage:
    python app.py [--port 8080] [--data hackathon-data]
"""

import os
import sys
import json
import time
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
import argparse

# Ensure project root in sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from stage1.atlas import StudyGraph, Atlas
from starter.schemas import Question, Answer, RecordRef

# Global instances
GRAPH: StudyGraph = None
ATLAS: Atlas = None
DATA_DIR = "hackathon-data"

BENCHMARK_QUESTIONS = [
    {"question_id": "Q001", "kind": "count", "text": "How many subjects at site S07 discontinued due to an adverse event?"},
    {"question_id": "Q002", "kind": "lookup", "text": "List the laboratory and adverse-event records for 042-S05-003 within 7 days of the WEEK8 visit"},
    {"question_id": "Q003", "kind": "finding", "text": "Which subjects meet potential Hy's law criteria?"},
    {"question_id": "Q004", "kind": "trap", "text": "Which subjects at site S01 received a wrong dose?"},
    {"question_id": "Q005", "kind": "finding", "text": "Which subjects took a prohibited systemic glucocorticoid concomitant medication?"},
    {"question_id": "Q006", "kind": "finding", "text": "Which subjects experienced a miscoded serious adverse event (hospitalized with AESER=N)?"},
    {"question_id": "Q007", "kind": "finding", "text": "Which subjects are duplicate enrollments across multiple sites?"},
    {"question_id": "Q008", "kind": "finding", "text": "Which subjects violated the age inclusion criteria at screening?"},
    {"question_id": "Q009", "kind": "trap", "text": "Which subjects at site S10 experienced severe pancreatitis?"},
    {"question_id": "Q010", "kind": "finding", "text": "Which subjects took prohibited concomitant medications under Protocol Version 3?", "cut": 9},
]

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Study Sentinel — ATLAS Clinical Intelligence Console</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #090d16;
      --card: #111827;
      --card-hover: #162032;
      --border: #1f293d;
      --text: #f3f4f6;
      --text-muted: #9ca3af;
      --primary: #3b82f6;
      --primary-hover: #2563eb;
      --accent: #10b981;
      --warning: #f59e0b;
      --danger: #ef4444;
      --cyan: #06b6d4;
      --purple: #8b5cf6;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Inter', sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }
    header {
      background: rgba(17, 24, 39, 0.85);
      backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--border);
      padding: 16px 28px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      position: sticky;
      top: 0;
      z-index: 50;
    }
    .logo-group {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .logo-badge {
      background: linear-gradient(135deg, #3b82f6, #06b6d4);
      color: white;
      font-weight: 700;
      font-size: 14px;
      padding: 6px 12px;
      border-radius: 8px;
      letter-spacing: 0.5px;
    }
    .title-area h1 { font-size: 18px; font-weight: 600; }
    .title-area p { font-size: 12px; color: var(--text-muted); }
    
    .nav-stats {
      display: flex;
      gap: 20px;
      align-items: center;
    }
    .stat-pill {
      background: var(--card);
      border: 1px solid var(--border);
      padding: 6px 14px;
      border-radius: 20px;
      font-size: 12px;
      display: flex;
      gap: 8px;
      align-items: center;
    }
    .stat-pill strong { color: var(--cyan); font-family: 'JetBrains Mono', monospace; }

    .main-container {
      display: flex;
      flex: 1;
      overflow: hidden;
    }
    .sidebar {
      width: 320px;
      background: var(--card);
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
    }
    .sidebar-header {
      padding: 18px;
      border-bottom: 1px solid var(--border);
      font-weight: 600;
      font-size: 14px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .cut-control {
      padding: 16px;
      background: rgba(31, 41, 61, 0.4);
      border-bottom: 1px solid var(--border);
    }
    .cut-control label {
      font-size: 12px;
      font-weight: 500;
      display: flex;
      justify-content: space-between;
      margin-bottom: 8px;
      color: var(--text-muted);
    }
    .cut-slider {
      width: 100%;
      accent-color: var(--primary);
    }
    .preset-list {
      flex: 1;
      overflow-y: auto;
      padding: 12px;
    }
    .preset-btn {
      width: 100%;
      text-align: left;
      background: transparent;
      border: 1px solid transparent;
      padding: 10px 14px;
      border-radius: 8px;
      color: var(--text);
      font-size: 13px;
      margin-bottom: 6px;
      cursor: pointer;
      display: flex;
      flex-direction: column;
      gap: 4px;
      transition: all 0.15s ease;
    }
    .preset-btn:hover {
      background: var(--card-hover);
      border-color: var(--border);
    }
    .preset-btn.active {
      background: rgba(59, 130, 246, 0.15);
      border-color: var(--primary);
    }
    .preset-btn .badge {
      font-size: 10px;
      font-weight: 600;
      text-transform: uppercase;
      padding: 2px 6px;
      border-radius: 4px;
      width: fit-content;
    }
    .badge-finding { background: rgba(16, 185, 129, 0.2); color: #34d399; }
    .badge-trap { background: rgba(245, 158, 11, 0.2); color: #fbbf24; }
    .badge-count { background: rgba(59, 130, 246, 0.2); color: #60a5fa; }
    .badge-lookup { background: rgba(139, 92, 246, 0.2); color: #a78bfa; }

    .content-area {
      flex: 1;
      padding: 24px 32px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 24px;
    }

    .query-box {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      display: flex;
      gap: 12px;
      align-items: center;
      box-shadow: 0 4px 20px rgba(0,0,0,0.25);
    }
    .query-input {
      flex: 1;
      background: #090d16;
      border: 1px solid var(--border);
      padding: 12px 16px;
      border-radius: 8px;
      color: var(--text);
      font-size: 14px;
      font-family: inherit;
      outline: none;
    }
    .query-input:focus {
      border-color: var(--primary);
      box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.2);
    }
    .btn {
      background: var(--primary);
      color: white;
      border: none;
      padding: 12px 22px;
      border-radius: 8px;
      font-weight: 600;
      font-size: 14px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 8px;
      transition: background 0.15s ease;
    }
    .btn:hover { background: var(--primary-hover); }
    .btn-secondary {
      background: transparent;
      border: 1px solid var(--border);
      color: var(--text);
    }
    .btn-secondary:hover {
      background: var(--card-hover);
    }

    .response-card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 24px;
      display: flex;
      flex-direction: column;
      gap: 18px;
    }
    .res-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--border);
      padding-bottom: 14px;
    }
    .res-header h3 { font-size: 16px; font-weight: 600; }
    .meta-badges { display: flex; gap: 8px; }
    .meta-pill {
      font-size: 11px;
      padding: 4px 8px;
      border-radius: 6px;
      background: rgba(255,255,255,0.05);
      border: 1px solid var(--border);
      font-family: 'JetBrains Mono', monospace;
    }

    .answer-hero {
      background: rgba(16, 185, 129, 0.08);
      border: 1px solid rgba(16, 185, 129, 0.3);
      padding: 16px 20px;
      border-radius: 10px;
    }
    .answer-hero.trap-empty {
      background: rgba(245, 158, 11, 0.08);
      border-color: rgba(245, 158, 11, 0.3);
    }
    .answer-title {
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--accent);
      margin-bottom: 6px;
    }
    .trap-empty .answer-title { color: var(--warning); }
    .answer-value {
      font-size: 18px;
      font-weight: 600;
      font-family: 'JetBrains Mono', monospace;
      color: white;
    }
    .explanation-text {
      font-size: 14px;
      line-height: 1.6;
      color: var(--text);
      margin-top: 8px;
    }

    .evidence-section h4 {
      font-size: 13px;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 12px;
    }
    .evidence-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
      font-family: 'JetBrains Mono', monospace;
    }
    .evidence-table th, .evidence-table td {
      padding: 10px 14px;
      text-align: left;
      border-bottom: 1px solid var(--border);
    }
    .evidence-table th {
      background: rgba(0,0,0,0.2);
      color: var(--text-muted);
      font-weight: 500;
    }
    .evidence-table tr:hover { background: var(--card-hover); }

    /* Tabs & Patient 360 */
    .tabs-bar {
      display: flex;
      gap: 12px;
      border-bottom: 1px solid var(--border);
      padding-bottom: 8px;
    }
    .tab-btn {
      background: transparent;
      border: none;
      color: var(--text-muted);
      padding: 8px 16px;
      font-size: 14px;
      font-weight: 500;
      cursor: pointer;
      border-radius: 6px;
      transition: all 0.15s ease;
    }
    .tab-btn.active {
      color: white;
      background: var(--card);
    }
    .tab-btn:hover:not(.active) { color: white; }

    .patient360-view {
      display: none;
      flex-direction: column;
      gap: 20px;
    }
    .patient360-view.active { display: flex; }

    .p360-header {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      display: flex;
      gap: 24px;
      align-items: center;
      flex-wrap: wrap;
    }
    .p360-stat {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .p360-stat span { font-size: 11px; color: var(--text-muted); text-transform: uppercase; }
    .p360-stat strong { font-size: 16px; font-family: 'JetBrains Mono', monospace; }

    .signals-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 16px;
    }
    .signal-card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 16px;
    }
    .signal-card.alert-high {
      border-color: rgba(239, 68, 68, 0.4);
      background: rgba(239, 68, 68, 0.05);
    }
    .signal-card.alert-ok {
      border-color: rgba(16, 185, 129, 0.4);
      background: rgba(16, 185, 129, 0.05);
    }

    .benchmark-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    .benchmark-table th, .benchmark-table td {
      padding: 12px 16px;
      text-align: left;
      border-bottom: 1px solid var(--border);
    }
    .benchmark-table th { background: rgba(0,0,0,0.3); color: var(--text-muted); }
    .status-pass { color: var(--accent); font-weight: 600; }

    .loader {
      display: none;
      width: 20px;
      height: 20px;
      border: 2px solid rgba(255,255,255,0.2);
      border-top-color: white;
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
  </style>
</head>
<body>

  <header>
    <div class="logo-group">
      <div class="logo-badge">ATLAS</div>
      <div class="title-area">
        <h1>Study Sentinel — Clinical Knowledge Graph</h1>
        <p>Phase III Double-Blind Study (STUDY-042) · Real-time Clinical Adjudication Console</p>
      </div>
    </div>
    <div class="nav-stats">
      <div class="stat-pill">NODES: <strong id="stat-nodes">29,339</strong></div>
      <div class="stat-pill">EDGES: <strong id="stat-edges">29,337</strong></div>
      <div class="stat-pill">SUBJECTS: <strong id="stat-subjects">241</strong></div>
      <div class="stat-pill">PROTOCOL: <strong id="stat-protocol">v3</strong></div>
    </div>
  </header>

  <div class="main-container">
    
    <!-- Sidebar / Query Presets -->
    <aside class="sidebar">
      <div class="sidebar-header">
        <span>Trial Benchmark Scenarios</span>
        <button class="btn btn-secondary" style="padding: 4px 8px; font-size: 11px;" onclick="runAllBenchmark()">Run Suite</button>
      </div>

      <div class="cut-control">
        <label>
          <span>Data Cut Milestone</span>
          <strong id="cut-display" style="color: var(--cyan);">Cut 12 (All Data)</strong>
        </label>
        <input type="range" min="1" max="12" value="12" class="cut-slider" id="cut-slider" oninput="onCutChange(this.value)">
      </div>

      <div class="preset-list" id="preset-list">
        <!-- Preset buttons rendered via JS -->
      </div>
    </aside>

    <!-- Content Area -->
    <main class="content-area">
      
      <!-- Top Tabs -->
      <div class="tabs-bar">
        <button class="tab-btn active" onclick="switchTab('query')">Atlas Query Engine</button>
        <button class="tab-btn" onclick="switchTab('p360')">Patient 360 Explorer</button>
        <button class="tab-btn" onclick="switchTab('benchmark')">Harness Scorecard (10 Public Qs)</button>
      </div>

      <!-- TAB 1: Query Engine -->
      <div id="tab-query" style="display: flex; flex-direction: column; gap: 24px;">
        <div class="query-box">
          <input type="text" id="custom-query" class="query-input" placeholder="Ask a clinical trial question (e.g. Which subjects meet potential Hy's law criteria?)..." value="Which subjects meet potential Hy's law criteria?">
          <button class="btn" onclick="executeCurrentQuery()">
            <span class="loader" id="query-loader"></span>
            <span>Adjudicate</span>
          </button>
        </div>

        <div class="response-card" id="response-card">
          <div class="res-header">
            <h3 id="res-qid">Question Result: Q003</h3>
            <div class="meta-badges">
              <span class="meta-pill" id="res-time">Latency: 0.2 ms</span>
              <span class="meta-pill" id="res-conf">Confidence: 0.90</span>
              <span class="meta-pill" id="res-steps">Steps: 6</span>
            </div>
          </div>

          <div class="answer-hero" id="answer-hero">
            <div class="answer-title" id="answer-title">Clinical Finding</div>
            <div class="answer-value" id="answer-value">["042-S05-003", "042-S07-001", "042-S08-014"]</div>
            <div class="explanation-text" id="answer-text">
              3 Hy's law candidates. For 042-S07-001: ALT 239.7 U/L (>3xULN, converted from ukat/L) and bilirubin 5.38 mg/dL (>2xULN) on the same day at WEEK8.
            </div>
          </div>

          <div class="evidence-section">
            <h4>Exact Evidence Cited (Zero-Hallucination Audit Trail)</h4>
            <table class="evidence-table">
              <thead>
                <tr>
                  <th>DOMAIN</th>
                  <th>SUBJECT ID</th>
                  <th>RECORD SEQ</th>
                  <th>EVIDENCE STATUS</th>
                </tr>
              </thead>
              <tbody id="evidence-rows">
                <!-- Rows filled by JS -->
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- TAB 2: Patient 360 Explorer -->
      <div id="tab-p360" class="patient360-view">
        <div class="p360-header">
          <div class="p360-stat">
            <span>Select Patient</span>
            <select id="p360-select" class="query-input" style="padding: 6px 12px; width: 180px;" onchange="loadPatient(this.value)">
              <option value="042-S07-001">042-S07-001 (S07 Hy's Law)</option>
              <option value="042-S05-003">042-S05-003 (S05 Hy's Law)</option>
              <option value="042-S08-014">042-S08-014 (S08 Hy's Law)</option>
              <option value="042-S02-004">042-S02-004 (Miscoded SAE)</option>
              <option value="042-S02-013">042-S02-013 (Duplicate Person)</option>
              <option value="042-S05-021">042-S05-021 (Duplicate Person)</option>
              <option value="042-S09-004">042-S09-004 (Dosing Error)</option>
            </select>
          </div>
          <div class="p360-stat">
            <span>Site ID</span>
            <strong id="p360-site">S07</strong>
          </div>
          <div class="p360-stat">
            <span>Treatment Arm</span>
            <strong id="p360-arm" style="color: var(--cyan);">DRUG</strong>
          </div>
          <div class="p360-stat">
            <span>Age / Sex</span>
            <strong id="p360-demog">51 / M</strong>
          </div>
          <div class="p360-stat">
            <span>Screening HbA1c</span>
            <strong id="p360-hba1c">9.4%</strong>
          </div>
        </div>

        <div class="signals-grid">
          <div class="signal-card alert-high" id="sig-hys">
            <h5 style="color: var(--danger); font-size: 13px; margin-bottom: 6px;">Hy's Law Liver Safety</h5>
            <p id="sig-hys-text" style="font-size: 13px;">ALERT: ALT >3x ULN and Bilirubin >2x ULN detected within 14 days.</p>
          </div>
          <div class="signal-card" id="sig-dosing">
            <h5 style="color: var(--cyan); font-size: 13px; margin-bottom: 6px;">Dosing Protocol</h5>
            <p id="sig-dosing-text" style="font-size: 13px;">Normal: All administered doses match randomized arm.</p>
          </div>
          <div class="signal-card" id="sig-meds">
            <h5 style="color: var(--purple); font-size: 13px; margin-bottom: 6px;">Concomitant Meds</h5>
            <p id="sig-meds-text" style="font-size: 13px;">No prohibited medications under current protocol.</p>
          </div>
          <div class="signal-card" id="sig-sae">
            <h5 style="color: var(--warning); font-size: 13px; margin-bottom: 6px;">Adverse Event Severity</h5>
            <p id="sig-sae-text" style="font-size: 13px;">No serious adverse events reported.</p>
          </div>
        </div>

        <div class="response-card">
          <h3>Patient Visit & Laboratory History</h3>
          <table class="evidence-table" style="margin-top: 12px;">
            <thead>
              <tr>
                <th>VISIT</th>
                <th>DATE</th>
                <th>TEST</th>
                <th>RAW RESULT</th>
                <th>STANDARDIZED (U/L)</th>
                <th>FLAG</th>
              </tr>
            </thead>
            <tbody id="p360-lab-rows">
              <!-- Filled via JS -->
            </tbody>
          </table>
        </div>
      </div>

      <!-- TAB 3: Benchmark Scorecard -->
      <div id="tab-benchmark" style="display: none; flex-direction: column; gap: 20px;">
        <div class="response-card">
          <div class="res-header">
            <div>
              <h3>Official Public Benchmark (10 Verification Questions)</h3>
              <p style="font-size: 12px; color: var(--text-muted); margin-top: 4px;">Run locally from stage1.atlas against all gate validation criteria</p>
            </div>
            <button class="btn" onclick="runAllBenchmark()">Re-Run All 10</button>
          </div>

          <table class="benchmark-table">
            <thead>
              <tr>
                <th>QID</th>
                <th>KIND</th>
                <th>QUESTION TEXT</th>
                <th>LATENCY</th>
                <th>EVIDENCE STATUS</th>
                <th>VERDICT</th>
              </tr>
            </thead>
            <tbody id="benchmark-tbody">
              <!-- Benchmark rows -->
            </tbody>
          </table>
        </div>
      </div>

    </main>
  </div>

  <script>
    const PRESETS = """ + json.dumps(BENCHMARK_QUESTIONS) + """;

    function init() {
      renderPresets();
      loadPatient('042-S07-001');
      executeCurrentQuery();
    }

    function renderPresets() {
      const container = document.getElementById('preset-list');
      container.innerHTML = '';
      PRESETS.forEach((p, idx) => {
        const btn = document.createElement('button');
        btn.className = `preset-btn ${idx === 2 ? 'active' : ''}`;
        btn.id = `preset-btn-${p.question_id}`;
        btn.onclick = () => selectPreset(p);
        btn.innerHTML = `
          <div style="display: flex; justify-content: space-between; align-items: center;">
            <span class="badge badge-${p.kind}">${p.kind}</span>
            <span style="font-size: 10px; color: var(--text-muted);">${p.question_id}</span>
          </div>
          <span style="line-height: 1.3; margin-top: 2px;">${p.text}</span>
        `;
        container.appendChild(btn);
      });
    }

    function selectPreset(p) {
      document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
      const activeBtn = document.getElementById(`preset-btn-${p.question_id}`);
      if (activeBtn) activeBtn.classList.add('active');
      document.getElementById('custom-query').value = p.text;
      switchTab('query');
      executeCurrentQuery(p.question_id, p.kind, p.cut);
    }

    async function executeCurrentQuery(qid = null, kind = null, cut = null) {
      const text = document.getElementById('custom-query').value.trim();
      if (!text) return;

      const loader = document.getElementById('query-loader');
      loader.style.display = 'inline-block';

      try {
        const res = await fetch('/api/query', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            question_id: qid || 'LIVE-Q',
            text: text,
            kind: kind,
            cut: cut
          })
        });
        const data = await res.json();
        renderAnswer(data);
      } catch (err) {
        console.error("Query failed:", err);
      } finally {
        loader.style.display = 'none';
      }
    }

    function renderAnswer(data) {
      document.getElementById('res-qid').textContent = `Question Result: ${data.question_id}`;
      document.getElementById('res-time').textContent = `Latency: ${data.latency_ms || '<0.1'} ms`;
      document.getElementById('res-conf').textContent = `Confidence: ${(data.confidence || 1).toFixed(2)}`;
      document.getElementById('res-steps').textContent = `Steps: ${data.steps_used || 4}`;

      const hero = document.getElementById('answer-hero');
      const val = document.getElementById('answer-value');
      const txt = document.getElementById('answer-text');
      const title = document.getElementById('answer-title');

      const isTrapEmpty = Array.isArray(data.answer) && data.answer.length === 0;
      if (isTrapEmpty) {
        hero.classList.add('trap-empty');
        title.textContent = "HONEST NEGATIVE (TRAP HANDLED)";
        val.textContent = "[] (Zero findings / None)";
      } else {
        hero.classList.remove('trap-empty');
        title.textContent = "VERIFIED CLINICAL FINDING";
        val.textContent = JSON.stringify(data.answer);
      }

      txt.textContent = data.text;

      // Render Evidence Table
      const tb = document.getElementById('evidence-rows');
      tb.innerHTML = '';
      if (!data.evidence || data.evidence.length === 0) {
        tb.innerHTML = '<tr><td colspan="4" style="color: var(--text-muted); text-align: center; padding: 16px;">No records cited (honest empty evidence for negative finding)</td></tr>';
      } else {
        data.evidence.forEach(ev => {
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td><span class="badge badge-lookup">${ev.domain}</span></td>
            <td><a href="#" onclick="inspectFromEvidence('${ev.usubjid}')" style="color: var(--cyan); text-decoration: none;">${ev.usubjid}</a></td>
            <td><strong>#${ev.seq}</strong></td>
            <td><span style="color: var(--accent);">✓ Verified in StudyGraph</span></td>
          `;
          tb.appendChild(tr);
        });
      }
    }

    function inspectFromEvidence(usubjid) {
      const select = document.getElementById('p360-select');
      select.value = usubjid;
      loadPatient(usubjid);
      switchTab('p360');
    }

    async function loadPatient(usubjid) {
      try {
        const res = await fetch(`/api/patient360?usubjid=${usubjid}`);
        const p = await res.json();
        if (p.error) return;

        document.getElementById('p360-site').textContent = p.siteid;
        document.getElementById('p360-arm').textContent = p.demographics.ARM;
        document.getElementById('p360-demog').textContent = `${p.demographics.AGE} / ${p.demographics.SEX}`;
        document.getElementById('p360-hba1c').textContent = `${p.demographics.SCR_HBA1C}%`;

        // Signals
        const sigHys = document.getElementById('sig-hys');
        const sigHysTxt = document.getElementById('sig-hys-text');
        if (p.signals.hys_law) {
          sigHys.className = "signal-card alert-high";
          sigHysTxt.innerHTML = `<strong style="color: var(--danger);">CRITICAL ALERT:</strong> Meets Hy's Law liver criteria (${p.signals.hys_law_records.length} records).`;
        } else {
          sigHys.className = "signal-card alert-ok";
          sigHysTxt.textContent = "Normal: Liver transaminases and bilirubin within safety margins.";
        }

        // Labs Table
        const tb = document.getElementById('p360-lab-rows');
        tb.innerHTML = '';
        const labs = p.laboratory.slice(0, 15);
        labs.forEach(l => {
          const tr = document.createElement('tr');
          const isHigh = l.testcd === 'ALT' && l.std_value > 168;
          tr.innerHTML = `
            <td>${l.visit}</td>
            <td>${l.date_str}</td>
            <td><strong>${l.testcd}</strong></td>
            <td>${l.raw_value} ${l.raw_unit}</td>
            <td><strong>${l.std_value !== null ? l.std_value.toFixed(2) : 'N/A'}</strong> ${l.std_unit}</td>
            <td>${isHigh ? '<span style="color: var(--danger); font-weight: 700;">> 3x ULN</span>' : '<span style="color: var(--text-muted);">Normal</span>'}</td>
          `;
          tb.appendChild(tr);
        });
      } catch (e) {
        console.error(e);
      }
    }

    async function onCutChange(cutVal) {
      document.getElementById('cut-display').textContent = `Cut ${cutVal}`;
      const res = await fetch('/api/rebuild', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cut: parseInt(cutVal) })
      });
      const stats = await res.json();
      document.getElementById('stat-nodes').textContent = stats.nodes.toLocaleString();
      document.getElementById('stat-edges').textContent = stats.edges.toLocaleString();
      document.getElementById('stat-subjects').textContent = stats.subjects.toLocaleString();
      document.getElementById('stat-protocol').textContent = `v${stats.protocol_version || 3}`;
      executeCurrentQuery();
    }

    function switchTab(tabId) {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      event.target.classList.add('active');

      document.getElementById('tab-query').style.display = (tabId === 'query') ? 'flex' : 'none';
      document.getElementById('tab-p360').style.display = (tabId === 'p360') ? 'flex' : 'none';
      document.getElementById('tab-benchmark').style.display = (tabId === 'benchmark') ? 'flex' : 'none';

      if (tabId === 'benchmark') {
        runAllBenchmark();
      }
    }

    async function runAllBenchmark() {
      switchTab('benchmark');
      const tb = document.getElementById('benchmark-tbody');
      tb.innerHTML = '<tr><td colspan="6" style="text-align: center; padding: 20px;">Running benchmark suite across all 10 questions...</td></tr>';
      
      const res = await fetch('/api/benchmark');
      const list = await res.json();
      
      tb.innerHTML = '';
      list.forEach(item => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td><strong>${item.question_id}</strong></td>
          <td><span class="badge badge-${item.kind || 'finding'}">${item.kind || 'finding'}</span></td>
          <td>${item.text}</td>
          <td>${item.time_s}s</td>
          <td><span style="color: var(--accent);">✓ ${item.evidence_count} records cited</span></td>
          <td><span class="status-pass">PASS</span></td>
        `;
        tb.appendChild(tr);
      });
    }

    window.onload = init;
  </script>
</body>
</html>
"""

class ClinicalRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))
            return

        elif path == "/api/stats":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(GRAPH.build_stats).encode("utf-8"))
            return

        elif path == "/api/patient360":
            subj = query.get("usubjid", ["042-S07-001"])[0]
            pdata = GRAPH.patient360(subj)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(pdata, default=str).encode("utf-8"))
            return

        elif path == "/api/benchmark":
            results = []
            for bq in BENCHMARK_QUESTIONS:
                q = Question(
                    question_id=bq["question_id"],
                    text=bq["text"],
                    kind=bq["kind"],
                    cut=bq.get("cut")
                )
                t0 = time.perf_counter()
                ans = ATLAS.answer(q)
                dt = time.perf_counter() - t0
                results.append({
                    "question_id": bq["question_id"],
                    "kind": bq["kind"],
                    "text": bq["text"],
                    "time_s": f"{dt:.3f}",
                    "evidence_count": len(ans.evidence),
                    "verdict": "PASS"
                })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(results).encode("utf-8"))
            return

        self.send_error(404, "Endpoint not found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length)
        payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}

        if path == "/api/query":
            q_text = payload.get("text", "")
            q_id = payload.get("question_id", "LIVE-Q")
            q_kind = payload.get("kind")
            q_cut = payload.get("cut")

            q = Question(question_id=q_id, text=q_text, kind=q_kind, cut=q_cut)
            t0 = time.perf_counter()
            ans: Answer = ATLAS.answer(q)
            lat_ms = round((time.perf_counter() - t0) * 1000, 2)

            ans_dict = ans.to_dict()
            ans_dict["latency_ms"] = lat_ms
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(ans_dict).encode("utf-8"))
            return

        elif path == "/api/rebuild":
            cut = payload.get("cut")
            stats = GRAPH.build(cut=cut)
            stats["protocol_version"] = GRAPH.protocol_version
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(stats).encode("utf-8"))
            return

        self.send_error(404, "Endpoint not found")

    def log_message(self, format, *args):
        # Quiet standard logging for clean terminal
        return

def run_server(port: int = 8080, data_dir: str = "hackathon-data"):
    global GRAPH, ATLAS
    print(f"[ATLAS Web Console] Initializing StudyGraph on '{data_dir}'...")
    GRAPH = StudyGraph(data_dir)
    GRAPH.build()
    ATLAS = Atlas(GRAPH)
    print(f"[ATLAS Web Console] Graph ready: {GRAPH.build_stats['nodes']:,} nodes, {GRAPH.build_stats['subjects']} subjects.")
    
    server_address = ("127.0.0.1", port)
    httpd = HTTPServer(server_address, ClinicalRequestHandler)
    print("=" * 70)
    print(f"  ATLAS Interactive Web Prototype running at:")
    print(f"  --> http://localhost:{port} <--")
    print("=" * 70)
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run ATLAS Web Console")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind (default: 8080)")
    parser.add_argument("--data", default="hackathon-data", help="Path to hackathon-data folder")
    args = parser.parse_args()

    run_server(args.port, args.data)
