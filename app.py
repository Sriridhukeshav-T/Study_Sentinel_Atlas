"""
app.py - Simple, Clean Clinical Agent Web Console for Study Sentinel: ATLAS
Communicates the core clinical workflow:
  USER -> Ask ATLAS -> ATLAS searches StudyGraph -> Applies protocol rules -> ANSWER -> Supporting Evidence

Lightweight, self-hosted on Python standard library http.server.
Preserves all existing StudyGraph, Atlas, Question, Answer, and RecordRef logic.
"""

import os
import sys
import json
import time
import re
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

def enrich_record_ref(ref: RecordRef, graph: StudyGraph) -> dict:
    """
    Extracts authentic readable details for a RecordRef directly from the StudyGraph.
    Never invents or hallucinates any values.
    """
    subj = ref.usubjid
    dom = ref.domain
    seq = ref.seq
    pdata = graph.subjects.get(subj, {})
    
    test_field = "-"
    raw_val = "-"
    norm_val = None
    date_str = "-"
    visit_str = "-"
    
    if dom == "LB":
        for r in pdata.get("laboratory", []):
            if r["seq"] == seq:
                test_field = r.get("testcd", "-")
                u = r.get("raw_unit", "")
                raw_val = f"{r.get('raw_value', '')} {u}".strip()
                if r.get("std_value") is not None:
                    norm_val = f"{r['std_value']:.2f} {r.get('std_unit', '')}".strip()
                date_str = r.get("date_str", "-")
                visit_str = r.get("visit", "-")
                break
    elif dom == "AE":
        for r in pdata.get("adverse_events", []):
            if r["seq"] == seq:
                test_field = r.get("term", "Adverse Event")
                sev = r.get("severity", "-")
                ser = "YES" if r.get("is_serious") else "NO"
                raw_val = f"Severity: {sev} | Serious: {ser}"
                if r.get("is_miscoded"):
                    norm_val = "MISCODED (Hospitalized with AESER=N)"
                date_str = r.get("date_str", "-")
                break
    elif dom == "EX":
        for r in pdata.get("exposure", []):
            if r["seq"] == seq:
                test_field = f"Dose Administration (Kit {r.get('kit', '-')})"
                raw_val = f"{r.get('dose', '-')} mg"
                if r.get("is_error"):
                    norm_val = "PROTOCOL VIOLATION (Wrong kit/dose)"
                date_str = r.get("date_str", "-")
                visit_str = r.get("visit", "-")
                break
    elif dom == "CM":
        for r in pdata.get("concomitant_medications", []):
            if r["seq"] == seq:
                test_field = r.get("treatment", "-")
                raw_val = f"Class: {r.get('med_class', '-')}"
                if r.get("is_prohibited"):
                    norm_val = "PROHIBITED MEDICATION"
                date_str = r.get("date_str", "-")
                break
    elif dom == "DM":
        dem = pdata.get("demographics", {})
        test_field = "Demographics Profile"
        raw_val = f"Arm: {dem.get('ARM', '-')} | Age: {dem.get('AGE', '-')} | Sex: {dem.get('SEX', '-')}"
        norm_val = f"Site: {pdata.get('siteid', '-')} | Initials: {dem.get('INITS', '-')} | DOB: {dem.get('BRTHDTC', '-')}"
        date_str = str(dem.get("RFSTDTC", "-"))
    elif dom == "DS":
        for r in pdata.get("disposition", []):
            if r["seq"] == seq:
                test_field = f"Disposition: {r.get('status', '-')}"
                raw_val = f"Reason: {r.get('reason', '-')}"
                date_str = r.get("date_str", "-")
                break
    else:
        test_field = dom
        raw_val = f"Record Seq #{seq}"
        date_str = "-"

    return {
        "subject": subj,
        "domain": dom,
        "seq": seq,
        "test": test_field,
        "value": raw_val,
        "normalized": norm_val,
        "date": date_str,
        "visit": visit_str,
        "record_ref": f"RecordRef(domain='{dom}', usubjid='{subj}', seq={seq})"
    }


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Study Sentinel — ATLAS Clinical Q&A Agent</title>
  <style>
    :root {
      --bg: #f8fafc;
      --surface: #ffffff;
      --surface-subtle: #f1f5f9;
      --border: #e2e8f0;
      --border-focus: #3b82f6;
      --text: #0f172a;
      --text-muted: #64748b;
      --primary: #2563eb;
      --primary-hover: #1d4ed8;
      --accent-green: #059669;
      --accent-green-bg: #ecfdf5;
      --accent-amber: #d97706;
      --accent-amber-bg: #fffbeb;
      --accent-red: #dc2626;
      --accent-red-bg: #fef2f2;
      --radius: 8px;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding: 0 0 60px 0;
    }

    /* Top Banner / Header */
    header {
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 24px 32px;
    }
    .header-content {
      max-width: 1040px;
      margin: 0 auto;
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 24px;
      flex-wrap: wrap;
    }
    .eyebrow {
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 1.2px;
      text-transform: uppercase;
      color: var(--primary);
      margin-bottom: 4px;
    }
    h1 {
      font-size: 26px;
      font-weight: 800;
      color: var(--text);
      letter-spacing: -0.5px;
      line-height: 1.2;
    }
    .subtitle {
      font-size: 15px;
      font-weight: 600;
      color: #334155;
      margin-top: 2px;
    }
    .tagline {
      font-size: 13.5px;
      color: var(--text-muted);
      margin-top: 6px;
      font-style: italic;
    }

    /* Small Sidebar / Status Box */
    .status-panel {
      background: var(--surface-subtle);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 12px 18px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      min-width: 200px;
    }
    .status-badge {
      display: flex;
      align-items: center;
      gap: 6px;
      font-weight: 700;
      font-size: 13px;
      color: var(--accent-green);
    }
    .status-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--accent-green);
      display: inline-block;
    }
    .stat-row {
      display: flex;
      justify-content: space-between;
      font-size: 12px;
      color: var(--text-muted);
    }
    .stat-row strong {
      color: var(--text);
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }

    /* Workflow Visual Indicator */
    .workflow-bar {
      max-width: 1040px;
      margin: 20px auto 0 auto;
      padding: 0 16px;
    }
    .workflow-container {
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 10px 16px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 12px;
      color: var(--text-muted);
      gap: 8px;
      overflow-x: auto;
    }
    .step-item {
      display: flex;
      align-items: center;
      gap: 6px;
      white-space: nowrap;
      font-weight: 500;
    }
    .step-item.highlight {
      color: var(--primary);
      font-weight: 700;
    }
    .step-arrow {
      color: #94a3b8;
      font-size: 13px;
    }

    /* Main Content Container */
    main {
      max-width: 1040px;
      margin: 24px auto 0 auto;
      padding: 0 16px;
      display: flex;
      flex-direction: column;
      gap: 24px;
    }

    /* Card Wrapper */
    .section-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 24px 28px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.03);
    }
    .section-heading {
      font-size: 14px;
      font-weight: 700;
      letter-spacing: 0.8px;
      text-transform: uppercase;
      color: #334155;
      margin-bottom: 14px;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    /* ASK ATLAS Form */
    .query-box {
      display: flex;
      gap: 12px;
      margin-bottom: 18px;
    }
    .query-input {
      flex: 1;
      padding: 14px 18px;
      font-size: 15px;
      border: 1.5px solid #cbd5e1;
      border-radius: var(--radius);
      color: var(--text);
      outline: none;
      transition: border-color 0.15s ease, box-shadow 0.15s ease;
      background: #ffffff;
    }
    .query-input:focus {
      border-color: var(--primary);
      box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.12);
    }
    .btn-ask {
      background: var(--primary);
      color: #ffffff;
      font-size: 14px;
      font-weight: 700;
      letter-spacing: 0.5px;
      padding: 0 28px;
      border: none;
      border-radius: var(--radius);
      cursor: pointer;
      transition: background-color 0.15s ease;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      min-width: 140px;
    }
    .btn-ask:hover {
      background: var(--primary-hover);
    }
    .btn-ask:disabled {
      background: #94a3b8;
      cursor: not-allowed;
    }

    /* Example questions */
    .examples-wrap {
      border-top: 1px solid var(--border);
      padding-top: 14px;
    }
    .examples-label {
      font-size: 12px;
      font-weight: 600;
      color: var(--text-muted);
      margin-bottom: 8px;
    }
    .examples-grid {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .chip-example {
      background: var(--surface-subtle);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 6px 12px;
      font-size: 12.5px;
      color: #334155;
      cursor: pointer;
      text-align: left;
      transition: all 0.15s ease;
    }
    .chip-example:hover {
      background: #e2e8f0;
      border-color: #cbd5e1;
      color: var(--text);
    }
    .chip-trap {
      border-color: #fed7aa;
      background: #fffaf5;
      color: #9a3412;
    }
    .chip-trap:hover {
      background: #ffedd5;
      border-color: #fdba74;
    }

    /* ATLAS RESPONSE Area */
    .response-meta {
      display: flex;
      gap: 16px;
      margin-bottom: 16px;
      flex-wrap: wrap;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      font-size: 11.5px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      padding: 3px 10px;
      border-radius: 4px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }
    .badge-finding { background: var(--accent-green-bg); color: var(--accent-green); border: 1px solid #a7f3d0; }
    .badge-trap { background: var(--accent-amber-bg); color: var(--accent-amber); border: 1px solid #fde68a; }
    .badge-count { background: #eff6ff; color: #2563eb; border: 1px solid #bfdbfe; }
    .badge-lookup { background: #f5f3ff; color: #7c3aed; border: 1px solid #ddd6fe; }
    .meta-pill {
      font-size: 12px;
      color: var(--text-muted);
      background: var(--surface-subtle);
      padding: 3px 10px;
      border-radius: 4px;
      border: 1px solid var(--border);
    }

    .q-display {
      font-size: 13px;
      color: var(--text-muted);
      margin-bottom: 12px;
    }
    .q-display strong {
      color: var(--text);
      font-size: 14px;
    }

    .answer-hero {
      background: #f8fafc;
      border: 1.5px solid #cbd5e1;
      border-left: 5px solid var(--primary);
      border-radius: var(--radius);
      padding: 18px 22px;
      margin-bottom: 20px;
    }
    .answer-hero.trap-mode {
      border-left-color: var(--accent-amber);
      background: #fffdfa;
    }
    .answer-label {
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      color: var(--text-muted);
      margin-bottom: 6px;
    }
    .answer-value {
      font-size: 20px;
      font-weight: 700;
      color: var(--text);
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      margin-bottom: 8px;
      word-break: break-word;
    }
    .answer-text {
      font-size: 14px;
      color: #334155;
      line-height: 1.6;
    }

    /* SUPPORTING EVIDENCE Section */
    .evidence-section {
      border-top: 1px solid var(--border);
      padding-top: 18px;
    }
    .evidence-count-badge {
      font-size: 12px;
      font-weight: normal;
      color: var(--text-muted);
      margin-left: 6px;
    }
    .evidence-list {
      display: flex;
      flex-direction: column;
      gap: 10px;
      margin-top: 12px;
    }
    .evidence-card {
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 12px 16px;
      display: grid;
      grid-template-columns: 140px 80px 180px 1fr 140px;
      gap: 12px;
      align-items: center;
      font-size: 12.5px;
      transition: border-color 0.15s ease;
    }
    .evidence-card:hover {
      border-color: #cbd5e1;
    }
    @media (max-width: 860px) {
      .evidence-card {
        grid-template-columns: 1fr 1fr;
        gap: 8px;
      }
    }
    .ev-subj { font-weight: 700; color: var(--text); font-family: ui-monospace, monospace; }
    .ev-domain {
      font-weight: 700;
      font-size: 11px;
      color: var(--primary);
      background: #eff6ff;
      border: 1px solid #bfdbfe;
      padding: 2px 6px;
      border-radius: 4px;
      width: fit-content;
      font-family: ui-monospace, monospace;
    }
    .ev-field { font-weight: 600; color: #1e293b; }
    .ev-val { color: #475569; }
    .ev-norm { font-weight: 700; color: var(--accent-green); }
    .ev-norm.alert { color: var(--accent-red); }
    .ev-date { color: var(--text-muted); font-size: 11.5px; }
    .ev-ref {
      grid-column: 1 / -1;
      font-family: ui-monospace, monospace;
      font-size: 11px;
      color: #64748b;
      background: #f8fafc;
      padding: 4px 8px;
      border-radius: 4px;
      border: 1px solid #f1f5f9;
      margin-top: 4px;
    }
    .empty-evidence {
      background: var(--surface-subtle);
      border: 1px dashed var(--border);
      border-radius: 6px;
      padding: 24px;
      text-align: center;
      color: var(--text-muted);
      font-size: 13.5px;
      font-style: italic;
    }

    /* PATIENT 360 Section */
    .patient-input-row {
      display: flex;
      gap: 10px;
      align-items: center;
      margin-bottom: 14px;
      flex-wrap: wrap;
    }
    .patient-input {
      padding: 10px 14px;
      font-size: 14px;
      border: 1.5px solid #cbd5e1;
      border-radius: var(--radius);
      color: var(--text);
      font-family: ui-monospace, monospace;
      outline: none;
      min-width: 220px;
    }
    .patient-input:focus {
      border-color: var(--primary);
    }
    .btn-patient {
      background: #334155;
      color: #ffffff;
      font-size: 13px;
      font-weight: 600;
      padding: 10px 18px;
      border: none;
      border-radius: var(--radius);
      cursor: pointer;
    }
    .btn-patient:hover {
      background: #1e293b;
    }
    .quick-bar {
      display: flex;
      gap: 6px;
      align-items: center;
      font-size: 12px;
      color: var(--text-muted);
    }
    .quick-chip {
      background: var(--surface-subtle);
      border: 1px solid var(--border);
      padding: 4px 8px;
      border-radius: 4px;
      cursor: pointer;
      font-family: ui-monospace, monospace;
      font-size: 11.5px;
      color: #334155;
    }
    .quick-chip:hover {
      background: #e2e8f0;
    }

    .patient-card {
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 18px 20px;
      display: flex;
      flex-direction: column;
      gap: 14px;
    }
    .patient-summary-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
      gap: 12px;
    }
    .p-stat {
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 10px 12px;
    }
    .p-stat-label { font-size: 11px; color: var(--text-muted); text-transform: uppercase; font-weight: 600; }
    .p-stat-val { font-size: 14px; font-weight: 700; color: var(--text); margin-top: 2px; }

    .signal-strip {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .sig-pill {
      font-size: 12px;
      font-weight: 600;
      padding: 4px 10px;
      border-radius: 4px;
      border: 1px solid transparent;
    }
    .sig-alert { background: var(--accent-red-bg); color: var(--accent-red); border-color: #fecaca; }
    .sig-ok { background: var(--accent-green-bg); color: var(--accent-green); border-color: #a7f3d0; }
    .sig-warn { background: var(--accent-amber-bg); color: var(--accent-amber); border-color: #fde68a; }

    .history-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: 6px;
      overflow: hidden;
    }
    .history-table th, .history-table td {
      padding: 8px 12px;
      text-align: left;
      border-bottom: 1px solid var(--border);
    }
    .history-table th {
      background: #f1f5f9;
      color: #475569;
      font-weight: 700;
      font-size: 11px;
      text-transform: uppercase;
    }
    .history-table tr:last-child td { border-bottom: none; }
  </style>
</head>
<body>

  <!-- Top Header -->
  <header>
    <div class="header-content">
      <div class="header-brand">
        <div class="eyebrow">STUDY SENTINEL</div>
        <h1>ATLAS</h1>
        <div class="subtitle">Study Knowledge Graph &amp; Question Answering Agent</div>
        <div class="tagline">“Ask questions about the clinical study and receive answers with supporting evidence.”</div>
      </div>

      <!-- Small Sidebar / Study Statistics -->
      <div class="status-panel">
        <div class="status-badge"><span class="status-dot"></span> ATLAS &bull; Ready</div>
        <div class="stat-row"><span>Subjects:</span> <strong id="stat-subjects">241</strong></div>
        <div class="stat-row"><span>Nodes:</span> <strong id="stat-nodes">29,339</strong></div>
        <div class="stat-row"><span>Edges:</span> <strong id="stat-edges">29,337</strong></div>
      </div>
    </div>
  </header>

  <!-- Core Workflow Breadcrumb -->
  <div class="workflow-bar">
    <div class="workflow-container">
      <div class="step-item"><span>USER</span></div>
      <div class="step-arrow">&rarr;</div>
      <div class="step-item"><span>Ask ATLAS a question</span></div>
      <div class="step-arrow">&rarr;</div>
      <div class="step-item"><span>ATLAS searches StudyGraph</span></div>
      <div class="step-arrow">&rarr;</div>
      <div class="step-item"><span>Applies study/protocol rules</span></div>
      <div class="step-arrow">&rarr;</div>
      <div class="step-item highlight"><span>ANSWER</span></div>
      <div class="step-arrow">&rarr;</div>
      <div class="step-item highlight"><span>Supporting Evidence</span></div>
    </div>
  </div>

  <!-- Main Interaction Area -->
  <main>

    <!-- SECTION 1: ASK ATLAS -->
    <section class="section-card">
      <h2 class="section-heading">ASK ATLAS</h2>
      
      <div class="query-box">
        <input type="text" id="q-input" class="query-input" placeholder="Type your question here..." value="Which subjects meet the Hy's law criteria?" onkeydown="if(event.key==='Enter') askAtlas()">
        <button class="btn-ask" id="btn-submit" onclick="askAtlas()">ASK ATLAS</button>
      </div>

      <!-- Example Questions -->
      <div class="examples-wrap">
        <div class="examples-label">Example questions (click to ask):</div>
        <div class="examples-grid">
          <button class="chip-example" onclick="runPreset('How many subjects were randomized?')">&bull; How many subjects were randomized?</button>
          <button class="chip-example" onclick="runPreset('Which subjects experienced a serious adverse event?')">&bull; Which subjects experienced a serious adverse event?</button>
          <button class="chip-example" onclick="runPreset('Which subjects meet the Hy\'s law criteria?')">&bull; Which subjects meet the Hy's law criteria?</button>
          <button class="chip-example" onclick="runPreset('Which subjects had dosing errors?')">&bull; Which subjects had dosing errors?</button>
          <button class="chip-example" onclick="runPreset('Which subjects had prohibited concomitant medication?')">&bull; Which subjects had prohibited concomitant medication?</button>
          <button class="chip-example chip-trap" onclick="runPreset('Which subjects at site S01 received a wrong dose?')">&bull; Trap: Which subjects at site S01 received a wrong dose?</button>
          <button class="chip-example" onclick="runPreset('Which subjects are duplicate enrollments across multiple sites?')">&bull; Which subjects are duplicate enrollments across multiple sites?</button>
        </div>
      </div>
    </section>

    <!-- SECTION 2: ATLAS RESPONSE & SUPPORTING EVIDENCE -->
    <section class="section-card" id="response-block">
      <h2 class="section-heading">ATLAS RESPONSE</h2>

      <div class="response-meta">
        <div><span class="meta-pill">Type: <strong id="res-type" style="color: var(--primary);">FINDING</strong></span></div>
        <div><span class="meta-pill">Confidence: <strong id="res-conf">0.90</strong></span></div>
        <div><span class="meta-pill">Latency: <strong id="res-time">&lt; 1 ms</strong></span></div>
      </div>

      <div class="q-display">
        Question: <strong id="res-qtext">Which subjects meet the Hy's law criteria?</strong>
      </div>

      <div class="answer-hero" id="hero-box">
        <div class="answer-label">Deterministic Answer</div>
        <div class="answer-value" id="res-answer">["042-S05-003", "042-S07-001", "042-S08-014"]</div>
        <div class="answer-text" id="res-text">
          3 Hy's law candidates. For 042-S07-001: ALT 239.7 U/L (>3xULN, converted from ukat/L) and bilirubin 5.38 mg/dL (>2xULN) at WEEK8.
        </div>
      </div>

      <!-- SUPPORTING EVIDENCE -->
      <div class="evidence-section">
        <h3 class="section-heading">
          SUPPORTING EVIDENCE
          <span class="evidence-count-badge" id="evidence-count">(3 records cited)</span>
        </h3>

        <div id="evidence-display" class="evidence-list">
          <!-- Rendered dynamically -->
        </div>
      </div>
    </section>

    <!-- SECTION 3: PATIENT 360 -->
    <section class="section-card">
      <h2 class="section-heading">Patient 360</h2>
      <p style="font-size: 13px; color: var(--text-muted); margin-bottom: 12px;">
        Inspect a concise summary of any subject in the study graph directly using <code>StudyGraph.patient360(usubjid)</code>.
      </p>

      <div class="patient-input-row">
        <input type="text" id="patient-input" class="patient-input" placeholder="Enter Subject ID (e.g. 042-S07-001)" value="042-S07-001" onkeydown="if(event.key==='Enter') fetchPatient()">
        <button class="btn-patient" onclick="fetchPatient()">VIEW PATIENT</button>
        <div class="quick-bar">
          <span>Quick view:</span>
          <button class="quick-chip" onclick="setPatient('042-S07-001')">042-S07-001 (Hy's Law)</button>
          <button class="quick-chip" onclick="setPatient('042-S05-003')">042-S05-003 (Hy's Law)</button>
          <button class="quick-chip" onclick="setPatient('042-S02-004')">042-S02-004 (Miscoded SAE)</button>
          <button class="quick-chip" onclick="setPatient('042-S09-004')">042-S09-004 (Dosing Error)</button>
          <button class="quick-chip" onclick="setPatient('042-S01-002')">042-S01-002 (Site S01 Clean)</button>
        </div>
      </div>

      <div id="patient-output">
        <!-- Filled by JS -->
      </div>
    </section>

  </main>

  <script>
    async function askAtlas() {
      const input = document.getElementById('q-input');
      const text = input.value.trim();
      if (!text) return;

      const btn = document.getElementById('btn-submit');
      btn.disabled = true;
      btn.textContent = "SEARCHING...";

      try {
        const resp = await fetch('/api/query', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: text })
        });
        const data = await resp.json();
        renderResponse(data);
      } catch (err) {
        console.error(err);
        alert("Error communicating with ATLAS backend.");
      } finally {
        btn.disabled = false;
        btn.textContent = "ASK ATLAS";
      }
    }

    function runPreset(text) {
      document.getElementById('q-input').value = text;
      askAtlas();
    }

    function renderResponse(data) {
      document.getElementById('res-qtext').textContent = data.question_text;
      document.getElementById('res-type').textContent = data.kind || 'FINDING';
      document.getElementById('res-conf').textContent = (data.confidence || 0.9).toFixed(2);
      document.getElementById('res-time').textContent = `${data.latency_ms || '< 1'} ms`;

      const hero = document.getElementById('hero-box');
      const valEl = document.getElementById('res-answer');
      const txtEl = document.getElementById('res-text');

      const isTrapEmpty = Array.isArray(data.answer) && data.answer.length === 0;
      if (isTrapEmpty) {
        hero.classList.add('trap-mode');
        valEl.textContent = "[] (Zero findings / None)";
      } else {
        hero.classList.remove('trap-mode');
        if (typeof data.answer === 'object') {
          valEl.textContent = JSON.stringify(data.answer);
        } else {
          valEl.textContent = String(data.answer);
        }
      }

      txtEl.textContent = data.text || "";

      // Evidence display
      const evCont = document.getElementById('evidence-display');
      const evCount = document.getElementById('evidence-count');
      evCont.innerHTML = "";

      const evidence = data.evidence || [];
      evCount.textContent = `(${evidence.length} record${evidence.length === 1 ? '' : 's'} cited)`;

      if (evidence.length === 0) {
        evCont.innerHTML = `<div class="empty-evidence">No matching evidence found.</div>`;
      } else {
        evidence.forEach(item => {
          const card = document.createElement('div');
          card.className = "evidence-card";
          
          let normHtml = "";
          if (item.normalized) {
            const isAlert = item.normalized.includes("PROHIBITED") || item.normalized.includes("VIOLATION") || item.normalized.includes("MISCODED");
            normHtml = `<div class="ev-norm ${isAlert ? 'alert' : ''}">Normalized: ${item.normalized}</div>`;
          }

          card.innerHTML = `
            <div>
              <div style="font-size: 11px; color: var(--text-muted);">Subject</div>
              <div class="ev-subj"><a href="javascript:void(0)" onclick="setPatient('${item.subject}')" style="color: var(--primary); text-decoration: underline;">${item.subject}</a></div>
            </div>
            <div>
              <div style="font-size: 11px; color: var(--text-muted);">Domain</div>
              <div class="ev-domain">${item.domain} #${item.seq}</div>
            </div>
            <div>
              <div style="font-size: 11px; color: var(--text-muted);">Test / Field</div>
              <div class="ev-field">${item.test}</div>
            </div>
            <div>
              <div style="font-size: 11px; color: var(--text-muted);">Recorded Value</div>
              <div class="ev-val">${item.value}</div>
              ${normHtml}
            </div>
            <div>
              <div style="font-size: 11px; color: var(--text-muted);">Date / Visit</div>
              <div class="ev-date">${item.date} ${item.visit && item.visit !== '-' ? '(' + item.visit + ')' : ''}</div>
            </div>
            <div class="ev-ref">Record: ${item.record_ref}</div>
          `;
          evCont.appendChild(card);
        });
      }
    }

    async function fetchPatient() {
      const subj = document.getElementById('patient-input').value.trim();
      if (!subj) return;

      const out = document.getElementById('patient-output');
      out.innerHTML = "<div style='color: var(--text-muted); font-size: 13px;'>Loading Patient 360...</div>";

      try {
        const resp = await fetch(`/api/patient360?usubjid=${encodeURIComponent(subj)}`);
        const p = await resp.json();
        if (p.error) {
          out.innerHTML = `<div class="empty-evidence">${p.error}</div>`;
          return;
        }

        const isHys = p.signals && p.signals.hys_law;
        const doseErrors = (p.signals && p.signals.dosing_errors) ? p.signals.dosing_errors.length : 0;
        const probMeds = (p.signals && p.signals.prohibited_meds) ? p.signals.prohibited_meds.length : 0;
        const saes = (p.signals && p.signals.sae) ? p.signals.sae.length : 0;

        let labsHtml = "";
        (p.laboratory || []).slice(0, 5).forEach(l => {
          labsHtml += `
            <tr>
              <td>${l.visit || '-'}</td>
              <td>${l.date_str || '-'}</td>
              <td><strong>${l.testcd}</strong></td>
              <td>${l.raw_value} ${l.raw_unit}</td>
              <td>${l.std_value !== null ? l.std_value.toFixed(2) + ' ' + l.std_unit : '-'}</td>
            </tr>
          `;
        });

        out.innerHTML = `
          <div class="patient-card">
            <div class="patient-summary-grid">
              <div class="p-stat">
                <div class="p-stat-label">Subject ID</div>
                <div class="p-stat-val">${p.usubjid}</div>
              </div>
              <div class="p-stat">
                <div class="p-stat-label">Site</div>
                <div class="p-stat-val">${p.siteid || '-'}</div>
              </div>
              <div class="p-stat">
                <div class="p-stat-label">Treatment Arm</div>
                <div class="p-stat-val" style="color: var(--primary);">${p.demographics ? p.demographics.ARM : '-'}</div>
              </div>
              <div class="p-stat">
                <div class="p-stat-label">Age / Sex</div>
                <div class="p-stat-val">${p.demographics ? p.demographics.AGE + ' / ' + p.demographics.SEX : '-'}</div>
              </div>
              <div class="p-stat">
                <div class="p-stat-label">Screening HbA1c</div>
                <div class="p-stat-val">${p.demographics ? p.demographics.SCR_HBA1C + '%' : '-'}</div>
              </div>
            </div>

            <div class="signal-strip">
              <span class="sig-pill ${isHys ? 'sig-alert' : 'sig-ok'}">
                Hy's Law: ${isHys ? 'CRITICAL ALERT (Met criteria)' : 'NORMAL'}
              </span>
              <span class="sig-pill ${doseErrors > 0 ? 'sig-alert' : 'sig-ok'}">
                Dosing: ${doseErrors > 0 ? doseErrors + ' Protocol Error(s)' : 'Adherent'}
              </span>
              <span class="sig-pill ${probMeds > 0 ? 'sig-warn' : 'sig-ok'}">
                Prohibited Meds: ${probMeds > 0 ? probMeds + ' Record(s)' : 'None'}
              </span>
              <span class="sig-pill ${saes > 0 ? 'sig-warn' : 'sig-ok'}">
                Serious AEs: ${saes > 0 ? saes + ' Event(s)' : 'None'}
              </span>
            </div>

            ${labsHtml ? `
              <div>
                <div style="font-size: 11px; font-weight: 700; text-transform: uppercase; color: var(--text-muted); margin-bottom: 6px;">
                  Laboratory Timeline (Most Recent Tests)
                </div>
                <table class="history-table">
                  <thead>
                    <tr>
                      <th>Visit</th>
                      <th>Date</th>
                      <th>Test</th>
                      <th>Raw Result</th>
                      <th>Standardized</th>
                    </tr>
                  </thead>
                  <tbody>${labsHtml}</tbody>
                </table>
              </div>
            ` : ''}
          </div>
        `;
      } catch (e) {
        console.error(e);
        out.innerHTML = "<div class='empty-evidence'>Failed to load subject.</div>";
      }
    }

    function setPatient(subj) {
      document.getElementById('patient-input').value = subj;
      fetchPatient();
    }

    // Initial load
    window.onload = function() {
      askAtlas();
      fetchPatient();
    };
  </script>
</body>
</html>
"""

class AgentRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))
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

        self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length)
        payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}

        if path == "/api/query":
            q_text = payload.get("text", "").strip()
            q_id = payload.get("question_id", "Q-LIVE")

            # Determine kind heuristics if not provided
            q_lower = q_text.lower()
            q_kind = "finding"
            if "how many" in q_lower or "count" in q_lower:
                q_kind = "count"
            elif "within" in q_lower and "visit" in q_lower:
                q_kind = "lookup"
            elif "wrong dose" in q_lower and "s01" in q_lower:
                q_kind = "trap"
            elif "site s10" in q_lower and "pancreatitis" in q_lower:
                q_kind = "trap"

            q = Question(question_id=q_id, text=q_text, kind=q_kind)
            
            t0 = time.perf_counter()
            ans: Answer = ATLAS.answer(q)
            lat_ms = round((time.perf_counter() - t0) * 1000, 2)

            # Enrich each cited RecordRef with real, non-hallucinated data from the graph
            enriched_evidence = [enrich_record_ref(r, GRAPH) for r in ans.evidence]

            response_data = {
                "question_id": ans.question_id,
                "question_text": q_text,
                "kind": q_kind.upper(),
                "answer": ans.answer,
                "text": ans.text,
                "confidence": ans.confidence,
                "steps_used": ans.steps_used,
                "latency_ms": lat_ms,
                "evidence": enriched_evidence
            }

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response_data).encode("utf-8"))
            return

        self.send_error(404, "Not Found")

    def log_message(self, format, *args):
        # Quiet standard logging
        return

def run_server(port: int = 8080, data_dir: str = "hackathon-data"):
    global GRAPH, ATLAS
    print(f"[ATLAS Agent Server] Initializing StudyGraph on '{data_dir}'...")
    GRAPH = StudyGraph(data_dir)
    GRAPH.build()
    ATLAS = Atlas(GRAPH)
    print(f"[ATLAS Agent Server] Graph ready: {GRAPH.build_stats['nodes']:,} nodes, {GRAPH.build_stats['subjects']} subjects.")
    
    server_address = ("127.0.0.1", port)
    httpd = HTTPServer(server_address, AgentRequestHandler)
    print("=" * 70)
    print(f"  ATLAS Clinical Q&A Agent running at:")
    print(f"  --> http://localhost:{port} <--")
    print("=" * 70)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run ATLAS Agent Web Console")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind (default: 8080)")
    parser.add_argument("--data", default="hackathon-data", help="Path to hackathon-data folder")
    args = parser.parse_args()

    run_server(args.port, args.data)
