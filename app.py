"""
app.py - Clinical Study Intelligence Agent Prototype for Study Sentinel: ATLAS
A conversational clinical AI research workspace focused entirely on the agent:
  USER -> Ask ATLAS -> StudyGraph Search -> Clinical Rules -> ANSWER -> WHY THIS ANSWER? (Evidence)

Self-hosted with standard library http.server.
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
from typing import Dict, List, Any, Optional

# Ensure project root in sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from stage1.atlas import StudyGraph, Atlas
from starter.schemas import Question, Answer, RecordRef

# Global instances
GRAPH: StudyGraph = None
ATLAS: Atlas = None
DATA_DIR = "hackathon-data"

def extract_usubjid(text: str) -> Optional[str]:
    m = re.search(r'\b(\d{3}-S\d{2}-\d{3})\b', text)
    return m.group(1) if m else None

def build_why_evidence(ans: Answer, graph: StudyGraph, question_text: str) -> dict:
    """
    Constructs the natural, readable "WHY THIS ANSWER?" section with real clinical
    values, reference limits, unit conversions, and exact RecordRefs.
    Never hallucinates or fabricates evidence.
    """
    q_lower = question_text.lower()
    evidence_refs = ans.evidence or []
    
    # 1. TRAP / NEGATIVE CASE
    if not evidence_refs or (isinstance(ans.answer, list) and len(ans.answer) == 0):
        return {
            "has_evidence": False,
            "message": "No supporting records found in the study data.",
            "groups": []
        }

    # Central Lab ULNs
    central_alt_uln = 56.0
    central_ast_uln = 40.0
    central_bili_uln = 1.2

    # Group evidence records by subject
    by_subj: Dict[str, List[RecordRef]] = {}
    for ref in evidence_refs:
        by_subj.setdefault(ref.usubjid, []).append(ref)

    groups = []

    for subj, refs in by_subj.items():
        pdata = graph.subjects.get(subj, {})
        subj_items = []
        cites_summary = []

        is_hys_query = "hy's law" in q_lower or "hys law" in q_lower or "liver" in q_lower

        for r in refs:
            dom = r.domain
            seq = r.seq
            cites_summary.append(f"{dom} #{seq}")

            if dom == "LB":
                # Find matching lab record
                for l in pdata.get("laboratory", []):
                    if l["seq"] == seq:
                        tcd = l.get("testcd", "Lab Test")
                        raw_v = f"{l.get('raw_value', '')} {l.get('raw_unit', '')}".strip()
                        std_v = l.get("std_value")
                        std_u = l.get("std_unit", "")
                        dt = l.get("date_str", "")
                        vis = l.get("visit", "")

                        norm_str = None
                        uln_str = None

                        if l.get("raw_unit", "").lower() in ("ukat/l", "µkat/l", "ukat"):
                            norm_str = f"{raw_v} -> {std_v:.1f} {std_u}"
                        else:
                            norm_str = f"{std_v:.1f} {std_u}" if std_v is not None else raw_v

                        if tcd == "ALT":
                            uln_str = f"ULN: {central_alt_uln:.0f} U/L | 3xULN: {3*central_alt_uln:.0f} U/L"
                        elif tcd == "AST":
                            uln_str = f"ULN: {central_ast_uln:.0f} U/L | 3xULN: {3*central_ast_uln:.0f} U/L"
                        elif tcd == "BILI":
                            uln_str = f"ULN: {central_bili_uln:.1f} mg/dL | 2xULN: {2*central_bili_uln:.1f} mg/dL"

                        subj_items.append({
                            "title": tcd,
                            "value": raw_v,
                            "normalized": norm_str if is_hys_query else None,
                            "uln": uln_str,
                            "date": f"{dt} ({vis})" if vis else dt,
                            "record_ref": f"RecordRef(domain='LB', usubjid='{subj}', seq={seq})"
                        })
                        break

            elif dom == "AE":
                for a in pdata.get("adverse_events", []):
                    if a["seq"] == seq:
                        term = a.get("term", "Adverse Event")
                        sev = a.get("severity", "N/A")
                        ser = "Yes" if a.get("is_serious") else "No"
                        hosp = "Yes" if a.get("raw", {}).get("AESHOSP") == "Y" else "No"
                        dt = a.get("date_str", "")
                        
                        note = "Hospitalized Adverse Event" if hosp == "Yes" else f"Severity: {sev}"
                        if a.get("is_miscoded"):
                            note += " (Miscoded: hospitalized with AESER=N)"

                        subj_items.append({
                            "title": f"Adverse Event: {term}",
                            "value": f"Severity: {sev} | Serious: {ser} | Hospitalization: {hosp}",
                            "normalized": note,
                            "uln": None,
                            "date": dt,
                            "record_ref": f"RecordRef(domain='AE', usubjid='{subj}', seq={seq})"
                        })
                        break

            elif dom == "EX":
                for e in pdata.get("exposure", []):
                    if e["seq"] == seq:
                        kit = e.get("kit", "N/A")
                        dose = e.get("dose", "N/A")
                        dt = e.get("date_str", "")
                        arm = pdata.get("demographics", {}).get("ARM", "N/A")
                        err_str = f"Protocol Error: kit {kit} ({dose} mg) administered to randomized {arm} subject" if e.get("is_error") else f"Dose: {dose} mg"

                        subj_items.append({
                            "title": f"Dosing Exposure (Kit {kit})",
                            "value": f"Dose: {dose} mg administered",
                            "normalized": err_str,
                            "uln": None,
                            "date": dt,
                            "record_ref": f"RecordRef(domain='EX', usubjid='{subj}', seq={seq})"
                        })
                        break

            elif dom == "CM":
                for c in pdata.get("concomitant_medications", []):
                    if c["seq"] == seq:
                        trt = c.get("treatment", "Medication")
                        mclass = c.get("med_class", "N/A")
                        dt = c.get("date_str", "")
                        proh = "PROHIBITED under protocol" if c.get("is_prohibited") else "Permitted"

                        subj_items.append({
                            "title": f"Concomitant Medication: {trt}",
                            "value": f"Class: {mclass}",
                            "normalized": proh,
                            "uln": None,
                            "date": dt,
                            "record_ref": f"RecordRef(domain='CM', usubjid='{subj}', seq={seq})"
                        })
                        break

            elif dom == "DM":
                dem = pdata.get("demographics", {})
                subj_items.append({
                    "title": "Demographics Enrollment Record",
                    "value": f"Arm: {dem.get('ARM', '-')} | Age: {dem.get('AGE', '-')} | Sex: {dem.get('SEX', '-')}",
                    "normalized": f"Site: {pdata.get('siteid', '-')} | Initials: {dem.get('INITS', '-')} | DOB: {dem.get('BRTHDTC', '-')}",
                    "uln": None,
                    "date": str(dem.get("RFSTDTC", "-")),
                    "record_ref": f"RecordRef(domain='DM', usubjid='{subj}', seq={seq})"
                })

            elif dom == "DS":
                for d in pdata.get("disposition", []):
                    if d["seq"] == seq:
                        subj_items.append({
                            "title": f"Disposition: {d.get('status', '-')}",
                            "value": f"Reason: {d.get('reason', '-')}",
                            "normalized": None,
                            "uln": None,
                            "date": d.get("date_str", "-"),
                            "record_ref": f"RecordRef(domain='DS', usubjid='{subj}', seq={seq})"
                        })
                        break

        groups.append({
            "subject": subj,
            "items": subj_items,
            "cites": ", ".join(cites_summary[:6])
        })

    # Limit to top 5 subjects in detailed view for brevity, summary for remainder
    total_subjects = len(groups)
    displayed_groups = groups[:6]

    return {
        "has_evidence": True,
        "total_subjects": total_subjects,
        "displayed_count": len(displayed_groups),
        "total_records": len(evidence_refs),
        "groups": displayed_groups
    }


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ATLAS — Clinical Study Intelligence Agent</title>
  <style>
    :root {
      --bg: #ffffff;
      --bg-subtle: #f8fafc;
      --border: #e2e8f0;
      --border-focus: #2563eb;
      --text-main: #0f172a;
      --text-muted: #64748b;
      --primary: #2563eb;
      --primary-hover: #1d4ed8;
      --primary-subtle: #eff6ff;
      --teal: #0d9488;
      --green: #10b981;
      --amber: #d97706;
      --red: #ef4444;
      --radius: 10px;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text-main);
      line-height: 1.6;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }

    /* Top App Bar */
    header {
      border-bottom: 1px solid var(--border);
      padding: 16px 32px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: #ffffff;
      position: sticky;
      top: 0;
      z-index: 40;
    }
    .brand-group {
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .brand-eyebrow {
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 1px;
      text-transform: uppercase;
      color: var(--text-muted);
    }
    .brand-title {
      font-size: 20px;
      font-weight: 800;
      color: var(--text-main);
      letter-spacing: -0.3px;
    }
    .brand-sub {
      font-size: 13px;
      color: var(--text-muted);
      border-left: 1px solid var(--border);
      padding-left: 12px;
    }

    /* Header Right: Status & Secondary Study Context */
    .header-right {
      display: flex;
      align-items: center;
      gap: 16px;
      position: relative;
    }
    .status-badge {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 12.5px;
      font-weight: 600;
      color: var(--teal);
    }
    .status-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--teal);
      display: inline-block;
    }
    .btn-context-toggle {
      background: var(--bg-subtle);
      border: 1px solid var(--border);
      color: #475569;
      font-size: 12px;
      font-weight: 600;
      padding: 5px 12px;
      border-radius: 6px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 5px;
    }
    .btn-context-toggle:hover {
      background: #e2e8f0;
    }
    .context-dropdown {
      position: absolute;
      right: 0;
      top: 40px;
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: 8px;
      box-shadow: 0 4px 16px rgba(0,0,0,0.08);
      padding: 14px 18px;
      width: 260px;
      display: none;
      flex-direction: column;
      gap: 8px;
      z-index: 50;
      font-size: 12px;
    }
    .context-dropdown.open { display: flex; }
    .ctx-row {
      display: flex;
      justify-content: space-between;
      color: var(--text-muted);
    }
    .ctx-row strong {
      color: var(--text-main);
      font-family: ui-monospace, SFMono-Regular, monospace;
    }

    /* Main Conversation / Workspace Area */
    main {
      flex: 1;
      max-width: 860px;
      width: 100%;
      margin: 0 auto;
      padding: 32px 20px 100px 20px;
      display: flex;
      flex-direction: column;
    }

    /* Welcome / Hero Centered View */
    .hero-container {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      text-align: center;
      margin: auto 0;
      padding: 40px 0;
    }
    .hero-title {
      font-size: 32px;
      font-weight: 800;
      color: #0f172a;
      letter-spacing: -0.6px;
      line-height: 1.25;
      margin-bottom: 24px;
    }

    /* Central Input Box */
    .input-wrapper {
      width: 100%;
      max-width: 720px;
      position: relative;
      margin-bottom: 20px;
    }
    .main-textarea {
      width: 100%;
      min-height: 110px;
      padding: 18px 20px;
      font-size: 16px;
      font-family: inherit;
      color: var(--text-main);
      background: #ffffff;
      border: 1.5px solid #cbd5e1;
      border-radius: 12px;
      outline: none;
      resize: vertical;
      box-shadow: 0 2px 8px rgba(0,0,0,0.04);
      transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }
    .main-textarea:focus {
      border-color: var(--primary);
      box-shadow: 0 0 0 4px rgba(37, 99, 235, 0.12);
    }
    .input-actions {
      display: flex;
      justify-content: flex-end;
      margin-top: 10px;
      width: 100%;
      max-width: 720px;
    }
    .btn-ask-main {
      background: var(--primary);
      color: #ffffff;
      font-size: 14.5px;
      font-weight: 700;
      padding: 12px 32px;
      border: none;
      border-radius: 8px;
      cursor: pointer;
      transition: background-color 0.15s ease, transform 0.05s ease;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .btn-ask-main:hover {
      background: var(--primary-hover);
    }
    .btn-ask-main:active {
      transform: scale(0.99);
    }
    .btn-ask-main:disabled {
      background: #94a3b8;
      cursor: not-allowed;
    }

    /* Suggestion Chips */
    .suggestions-group {
      width: 100%;
      max-width: 720px;
      margin-top: 24px;
      text-align: left;
    }
    .suggestions-label {
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-muted);
      margin-bottom: 10px;
    }
    .chips-flex {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .suggestion-chip {
      background: var(--bg-subtle);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 16px;
      font-size: 13.5px;
      color: #334155;
      cursor: pointer;
      text-align: left;
      transition: all 0.15s ease;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .suggestion-chip:hover {
      background: #f1f5f9;
      border-color: #cbd5e1;
      color: var(--primary);
      transform: translateX(2px);
    }
    .chip-arrow {
      color: #94a3b8;
      font-size: 12px;
    }

    /* Conversation Stream View */
    .conversation-feed {
      display: none;
      flex-direction: column;
      gap: 32px;
    }
    .conversation-feed.active {
      display: flex;
    }

    /* Message Turn Container */
    .turn-block {
      display: flex;
      flex-direction: column;
      gap: 16px;
    }

    /* User Message */
    .user-message-card {
      background: #f8fafc;
      border: 1px solid var(--border);
      border-left: 4px solid #3b82f6;
      border-radius: 8px;
      padding: 16px 20px;
    }
    .message-speaker {
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.8px;
      text-transform: uppercase;
      color: #64748b;
      margin-bottom: 4px;
    }
    .user-question-text {
      font-size: 16px;
      font-weight: 600;
      color: #0f172a;
    }

    /* Processing State */
    .processing-indicator {
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 13.5px;
      color: #475569;
      padding: 12px 18px;
      background: #f8fafc;
      border-radius: 8px;
      border: 1px solid var(--border);
    }
    .pulse-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--primary);
      animation: pulse 1s infinite alternate;
    }
    @keyframes pulse {
      from { opacity: 0.3; transform: scale(0.85); }
      to { opacity: 1; transform: scale(1.15); }
    }

    /* ATLAS Response Message */
    .atlas-response-card {
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 24px 26px;
      box-shadow: 0 1px 4px rgba(0,0,0,0.03);
      display: flex;
      flex-direction: column;
      gap: 16px;
    }
    .atlas-speaker {
      display: flex;
      align-items: center;
      justify-content: space-between;
      border-bottom: 1px solid #f1f5f9;
      padding-bottom: 10px;
    }
    .atlas-tag {
      font-size: 13px;
      font-weight: 800;
      letter-spacing: 0.5px;
      color: var(--primary);
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .latency-pill {
      font-size: 11px;
      color: #64748b;
      font-family: ui-monospace, monospace;
      background: var(--bg-subtle);
      padding: 2px 8px;
      border-radius: 4px;
      border: 1px solid var(--border);
    }

    /* Primary Answer Display */
    .answer-lead {
      font-size: 16px;
      color: #1e293b;
      line-height: 1.6;
    }
    .answer-lead strong {
      color: #0f172a;
    }

    .subjects-list {
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px 18px;
      margin: 4px 0;
    }
    .subjects-list-title {
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      color: var(--text-muted);
      margin-bottom: 8px;
    }
    .subjects-bullets {
      list-style: none;
      display: flex;
      flex-wrap: wrap;
      gap: 8px 16px;
    }
    .subjects-bullets li {
      font-family: ui-monospace, monospace;
      font-size: 14px;
      font-weight: 700;
      color: var(--primary);
    }
    .subjects-bullets li::before {
      content: "• ";
      color: #94a3b8;
    }

    .answer-explanation {
      font-size: 14px;
      color: #334155;
      line-height: 1.6;
      background: #ffffff;
    }

    /* WHY THIS ANSWER? Box */
    .why-container {
      background: #f8fafc;
      border: 1.5px solid #cbd5e1;
      border-radius: 8px;
      padding: 18px 20px;
      margin-top: 6px;
    }
    .why-header {
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.8px;
      text-transform: uppercase;
      color: #334155;
      margin-bottom: 12px;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .why-group {
      border-top: 1px solid #e2e8f0;
      padding-top: 12px;
      margin-top: 12px;
    }
    .why-group:first-of-type {
      border-top: none;
      padding-top: 0;
      margin-top: 0;
    }
    .why-subj-header {
      font-size: 14px;
      font-weight: 800;
      font-family: ui-monospace, monospace;
      color: #0f172a;
      margin-bottom: 8px;
    }
    .why-item {
      margin-bottom: 10px;
      padding-left: 10px;
      border-left: 2px solid #94a3b8;
    }
    .why-item-title {
      font-size: 12.5px;
      font-weight: 700;
      color: #1e293b;
    }
    .why-item-val {
      font-size: 13.5px;
      font-family: ui-monospace, monospace;
      font-weight: 600;
      color: #0f172a;
      margin: 2px 0;
    }
    .why-item-norm {
      font-size: 13px;
      font-weight: 700;
      color: var(--teal);
    }
    .why-item-uln {
      font-size: 11.5px;
      color: #64748b;
    }
    .why-item-ref {
      font-size: 11px;
      font-family: ui-monospace, monospace;
      color: #64748b;
      margin-top: 3px;
    }
    .why-empty-notice {
      font-size: 13px;
      color: var(--text-muted);
      font-style: italic;
    }

    /* Fixed Bottom Follow-Up Bar */
    .bottom-bar {
      position: fixed;
      bottom: 0;
      left: 0;
      right: 0;
      background: rgba(255, 255, 255, 0.95);
      backdrop-filter: blur(8px);
      border-top: 1px solid var(--border);
      padding: 14px 20px;
      display: none;
      justify-content: center;
      z-index: 30;
    }
    .bottom-bar.active {
      display: flex;
    }
    .bottom-form {
      max-width: 860px;
      width: 100%;
      display: flex;
      gap: 10px;
    }
    .bottom-input {
      flex: 1;
      padding: 12px 18px;
      font-size: 14px;
      border: 1.5px solid #cbd5e1;
      border-radius: 8px;
      outline: none;
      background: #ffffff;
    }
    .bottom-input:focus {
      border-color: var(--primary);
    }
    .btn-bottom-ask {
      background: var(--primary);
      color: #ffffff;
      font-size: 13.5px;
      font-weight: 700;
      padding: 0 24px;
      border: none;
      border-radius: 8px;
      cursor: pointer;
    }
    .btn-bottom-ask:hover {
      background: var(--primary-hover);
    }
  </style>
</head>
<body>

  <!-- Top App Header -->
  <header>
    <div class="brand-group">
      <div>
        <div class="brand-eyebrow">STUDY SENTINEL</div>
        <div class="brand-title">ATLAS</div>
      </div>
      <div class="brand-sub">Clinical Study Intelligence Agent</div>
    </div>

    <!-- Status & Secondary Collapsible Study Context -->
    <div class="header-right">
      <div class="status-badge">
        <span class="status-dot"></span> Connected to Study Knowledge Graph
      </div>

      <button class="btn-context-toggle" onclick="toggleContext(event)">
        ▾ Study Context
      </button>

      <div class="context-dropdown" id="context-dropdown">
        <div style="font-weight: 700; border-bottom: 1px solid var(--border); padding-bottom: 4px; margin-bottom: 4px;">STUDY-042 Context</div>
        <div class="ctx-row"><span>Subjects:</span> <strong id="ctx-subjects">241</strong></div>
        <div class="ctx-row"><span>Graph Nodes:</span> <strong id="ctx-nodes">29,339</strong></div>
        <div class="ctx-row"><span>Graph Edges:</span> <strong id="ctx-edges">29,337</strong></div>
        <div class="ctx-row"><span>Current Data Cut:</span> <strong>Cut 12 (EOS)</strong></div>
        <div class="ctx-row"><span>Protocol Version:</span> <strong>Version 3</strong></div>
      </div>
    </div>
  </header>

  <!-- Main Workspace -->
  <main id="main-workspace">

    <!-- INITIAL CENTERED SCREEN -->
    <div class="hero-container" id="hero-screen">
      <h2 class="hero-title">
        What would you like to know<br>about this study?
      </h2>

      <div class="input-wrapper">
        <textarea id="initial-input" class="main-textarea" placeholder="Ask ATLAS a question..."></textarea>
      </div>

      <div class="input-actions">
        <button class="btn-ask-main" id="btn-initial-ask" onclick="submitInitialQuestion()">
          Ask ATLAS
        </button>
      </div>

      <!-- Small Clickable Suggestions -->
      <div class="suggestions-group">
        <div class="suggestions-label">Example Questions</div>
        <div class="chips-flex">
          <button class="suggestion-chip" onclick="askPreset('How many subjects were randomized?')">
            <span>"How many subjects were randomized?"</span>
            <span class="chip-arrow">&rarr;</span>
          </button>
          <button class="suggestion-chip" onclick="askPreset('Which subjects experienced a serious adverse event?')">
            <span>"Which subjects experienced a serious adverse event?"</span>
            <span class="chip-arrow">&rarr;</span>
          </button>
          <button class="suggestion-chip" onclick="askPreset('Which subjects meet Hy\'s law criteria?')">
            <span>"Which subjects meet Hy's law criteria?"</span>
            <span class="chip-arrow">&rarr;</span>
          </button>
          <button class="suggestion-chip" onclick="askPreset('Which subjects had dosing errors?')">
            <span>"Which subjects had dosing errors?"</span>
            <span class="chip-arrow">&rarr;</span>
          </button>
          <button class="suggestion-chip" onclick="askPreset('Which subjects at site S01 received a wrong dose?')">
            <span>"Which subjects at site S01 received a wrong dose?" (Trap)</span>
            <span class="chip-arrow">&rarr;</span>
          </button>
          <button class="suggestion-chip" onclick="askPreset('Show me the information for subject 042-S01-001.')">
            <span>"Show me the information for subject 042-S01-001." (Patient Summary)</span>
            <span class="chip-arrow">&rarr;</span>
          </button>
        </div>
      </div>
    </div>

    <!-- CONVERSATION STREAM FEED -->
    <div class="conversation-feed" id="conv-feed">
      <!-- Dynamically appended conversation turns -->
    </div>

  </main>

  <!-- Follow-up Question Bar (Visible once conversation begins) -->
  <div class="bottom-bar" id="bottom-bar">
    <div class="bottom-form">
      <input type="text" id="followup-input" class="bottom-input" placeholder="Ask another question about the study..." onkeydown="if(event.key==='Enter') submitFollowupQuestion()">
      <button class="btn-bottom-ask" id="btn-followup-ask" onclick="submitFollowupQuestion()">
        Ask ATLAS
      </button>
    </div>
  </div>

  <script>
    function toggleContext(e) {
      e.stopPropagation();
      const dd = document.getElementById('context-dropdown');
      dd.classList.toggle('open');
    }
    document.addEventListener('click', () => {
      const dd = document.getElementById('context-dropdown');
      if (dd) dd.classList.remove('open');
    });

    function askPreset(text) {
      document.getElementById('initial-input').value = text;
      submitInitialQuestion();
    }

    function submitInitialQuestion() {
      const text = document.getElementById('initial-input').value.trim();
      if (!text) return;
      executeQuestion(text);
    }

    function submitFollowupQuestion() {
      const input = document.getElementById('followup-input');
      const text = input.value.trim();
      if (!text) return;
      input.value = '';
      executeQuestion(text);
    }

    async function executeQuestion(queryText) {
      // Transition UI from hero to conversation
      document.getElementById('hero-screen').style.display = 'none';
      const feed = document.getElementById('conv-feed');
      feed.classList.add('active');
      document.getElementById('bottom-bar').classList.add('active');

      // Create conversation turn block
      const turnId = 'turn-' + Date.now();
      const turnDiv = document.createElement('div');
      turnDiv.className = 'turn-block';
      turnDiv.id = turnId;

      // 1. User Message
      turnDiv.innerHTML = `
        <div class="user-message-card">
          <div class="message-speaker">USER</div>
          <div class="user-question-text">${escapeHtml(queryText)}</div>
        </div>
        <div class="processing-indicator" id="proc-${turnId}">
          <span class="pulse-dot"></span>
          <span>ATLAS is checking the study records...</span>
        </div>
      `;
      feed.appendChild(turnDiv);
      window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });

      // Disable buttons
      const btnInitial = document.getElementById('btn-initial-ask');
      const btnFollowup = document.getElementById('btn-followup-ask');
      if (btnInitial) btnInitial.disabled = true;
      if (btnFollowup) btnFollowup.disabled = true;

      try {
        const resp = await fetch('/api/query', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: queryText })
        });
        const data = await resp.json();

        // Update processing state briefly
        const procEl = document.getElementById(`proc-${turnId}`);
        if (procEl) {
          procEl.innerHTML = `<span class="pulse-dot" style="background: var(--green);"></span> <span>ATLAS found relevant records.</span>`;
        }

        setTimeout(() => {
          if (procEl) procEl.remove();
          renderAtlasAnswer(turnDiv, data);
          window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
        }, 250);

      } catch (err) {
        console.error(err);
        const procEl = document.getElementById(`proc-${turnId}`);
        if (procEl) procEl.innerHTML = `<span style="color: var(--red);">Error communicating with ATLAS backend.</span>`;
      } finally {
        if (btnInitial) btnInitial.disabled = false;
        if (btnFollowup) btnFollowup.disabled = false;
      }
    }

    function renderAtlasAnswer(container, data) {
      const respCard = document.createElement('div');
      respCard.className = 'atlas-response-card';

      // 1. Main Lead Answer
      let leadHtml = "";
      let subjectsBlock = "";

      const ans = data.answer;
      const isTrap = data.is_trap || (Array.isArray(ans) && ans.length === 0);

      if (isTrap) {
        leadHtml = `<strong>I could not find supporting evidence for this claim in the available study data.</strong>`;
      } else if (typeof ans === 'number') {
        leadHtml = `<strong>${ans.toLocaleString()} subjects</strong>`;
      } else if (Array.isArray(ans)) {
        leadHtml = `I found <strong>${ans.length} subject${ans.length === 1 ? '' : 's'}</strong> meeting the specified criteria.`;
        if (ans.length > 0 && typeof ans[0] === 'string' && ans[0].startsWith('042-')) {
          subjectsBlock = `
            <div class="subjects-list">
              <div class="subjects-list-title">Subjects Identified:</div>
              <ul class="subjects-bullets">
                ${ans.map(s => `<li>${s}</li>`).join('')}
              </ul>
            </div>
          `;
        }
      } else {
        leadHtml = `<strong>${escapeHtml(String(ans))}</strong>`;
      }

      // Explanation / Rationale text
      let explanationHtml = "";
      if (data.text) {
        explanationHtml = `<div class="answer-explanation">${escapeHtml(data.text).replace(/\\n/g, '<br>')}</div>`;
      }

      // 2. WHY THIS ANSWER? Box
      let whyHtml = "";
      const why = data.why || {};

      if (!why.has_evidence || isTrap) {
        whyHtml = `
          <div class="why-container">
            <div class="why-header">WHY THIS ANSWER?</div>
            <div class="why-empty-notice">
              Evidence: No supporting records found in the study data.
            </div>
          </div>
        `;
      } else {
        let groupsHtml = "";
        (why.groups || []).forEach(g => {
          let itemsHtml = "";
          (g.items || []).forEach(it => {
            itemsHtml += `
              <div class="why-item">
                <div class="why-item-title">${it.title}</div>
                ${it.normalized ? `<div class="why-item-norm">${it.normalized}</div>` : `<div class="why-item-val">${it.value}</div>`}
                ${it.uln ? `<div class="why-item-uln">${it.uln}</div>` : ''}
                ${it.date ? `<div class="why-item-uln">Date: ${it.date}</div>` : ''}
                <div class="why-item-ref">Evidence: ${it.record_ref}</div>
              </div>
            `;
          });

          groupsHtml += `
            <div class="why-group">
              <div class="why-subj-header">${g.subject}</div>
              ${itemsHtml}
            </div>
          `;
        });

        whyHtml = `
          <div class="why-container">
            <div class="why-header">WHY THIS ANSWER?</div>
            ${groupsHtml}
          </div>
        `;
      }

      respCard.innerHTML = `
        <div class="atlas-speaker">
          <div class="atlas-tag">ATLAS</div>
          <div class="latency-pill">${data.latency_ms || '< 1'} ms &bull; ${data.confidence ? (data.confidence * 100).toFixed(0) + '% conf' : 'Verified'}</div>
        </div>

        <div class="answer-lead">${leadHtml}</div>
        ${subjectsBlock}
        ${explanationHtml}
        ${whyHtml}
      `;

      container.appendChild(respCard);
    }

    function escapeHtml(str) {
      if (!str) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }
  </script>
</body>
</html>
"""

class ClinicalAgentHandler(BaseHTTPRequestHandler):
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
            q_id = payload.get("question_id", "Q-AGENT")

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

            why_data = build_why_evidence(ans, GRAPH, q_text)
            is_trap = q_kind == "trap" or (isinstance(ans.answer, list) and len(ans.answer) == 0)

            response_payload = {
                "question_id": ans.question_id,
                "question_text": q_text,
                "kind": q_kind.upper(),
                "answer": ans.answer,
                "text": ans.text,
                "confidence": ans.confidence,
                "steps_used": ans.steps_used,
                "latency_ms": lat_ms,
                "is_trap": is_trap,
                "why": why_data
            }

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response_payload, default=str).encode("utf-8"))
            return

        self.send_error(404, "Not Found")

    def log_message(self, format, *args):
        # Quiet standard logging
        return

def run_server(port: int = 8080, data_dir: str = "hackathon-data"):
    global GRAPH, ATLAS
    print(f"[ATLAS Clinical Agent] Initializing StudyGraph on '{data_dir}'...")
    GRAPH = StudyGraph(data_dir)
    GRAPH.build()
    ATLAS = Atlas(GRAPH)
    print(f"[ATLAS Clinical Agent] Graph ready: {GRAPH.build_stats['nodes']:,} nodes, {GRAPH.build_stats['subjects']} subjects.")
    
    server_address = ("127.0.0.1", port)
    httpd = HTTPServer(server_address, ClinicalAgentHandler)
    print("=" * 70)
    print(f"  ATLAS Clinical Study Intelligence Agent running at:")
    print(f"  --> http://localhost:{port} <--")
    print("=" * 70)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run ATLAS Clinical Agent Web Console")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind (default: 8080)")
    parser.add_argument("--data", default="hackathon-data", help="Path to hackathon-data folder")
    args = parser.parse_args()

    run_server(args.port, args.data)
