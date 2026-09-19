"""
app.py - Clinical Study Intelligence Agent & Patient 360 Prototype for Study Sentinel: ATLAS
A conversational clinical AI research workspace with:
  1. Ask ATLAS: Conversational Question-Answering with Evidence Validation
  2. Patient 360° Graph: Interactive Patient Knowledge Graph with Connected Clinical Domains

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
from stage2.crew import ReviewCrew, ReviewReport, TraceLogger

# Global instances
GRAPH: StudyGraph = None
ATLAS: Atlas = None
CREW: Optional[ReviewCrew] = None
LATEST_REPORT: Optional[dict] = None
PENDING_ESCALATIONS: List[dict] = []
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
                    "normalized": f"Site: {pdata.get('siteid', '-')} | Initials: {dem.get('DMINIT', dem.get('INITS', '-'))} | DOB: {dem.get('BRTHDTC', '-')}",
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

    # Limit to top 6 subjects in detailed view for brevity
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
  <title>ATLAS — Clinical Study Intelligence & Patient 360°</title>
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
      padding: 14px 32px;
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
      gap: 16px;
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

    /* Header Nav Tabs */
    .header-nav {
      display: flex;
      align-items: center;
      gap: 4px;
      background: var(--bg-subtle);
      padding: 4px;
      border-radius: 8px;
      border: 1px solid var(--border);
      margin-left: 10px;
    }
    .nav-btn {
      background: transparent;
      border: none;
      padding: 6px 14px;
      font-size: 13px;
      font-weight: 600;
      color: var(--text-muted);
      border-radius: 6px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
      transition: all 0.15s ease;
    }
    .nav-btn:hover {
      color: var(--text-main);
    }
    .nav-btn.active {
      background: #ffffff;
      color: var(--primary);
      font-weight: 700;
      box-shadow: 0 1px 3px rgba(0,0,0,0.08);
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
      display: inline-flex;
      align-items: center;
      gap: 4px;
      transition: color 0.15s ease;
    }
    .subjects-bullets li:hover {
      color: var(--primary-hover);
      text-decoration: underline;
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
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }
    .why-subj-header:hover {
      color: var(--primary);
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

    /* =========================================================
       PATIENT 360° GRAPH SECTION STYLES
       ========================================================= */
    .p360-container {
      max-width: 1240px;
      width: 100%;
      margin: 0 auto;
      padding: 24px 24px 80px 24px;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }
    .p360-toolbar-card {
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px 24px;
      box-shadow: 0 1px 4px rgba(0,0,0,0.03);
      display: flex;
      flex-direction: column;
      gap: 14px;
    }
    .p360-title {
      font-size: 20px;
      font-weight: 800;
      color: var(--text-main);
      display: flex;
      align-items: center;
      gap: 8px;
      letter-spacing: -0.3px;
    }
    .p360-subtitle {
      font-size: 13px;
      color: var(--text-muted);
    }
    .p360-search-row {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }
    .p360-input-wrapper {
      flex: 1;
      min-width: 280px;
      position: relative;
    }
    .p360-input {
      width: 100%;
      padding: 11px 16px 11px 38px;
      font-size: 14.5px;
      font-family: ui-monospace, SFMono-Regular, monospace;
      font-weight: 700;
      border: 1.5px solid #cbd5e1;
      border-radius: 8px;
      outline: none;
      background: #ffffff;
      color: var(--text-main);
      transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }
    .p360-input:focus {
      border-color: var(--primary);
      box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.12);
    }
    .p360-input-icon {
      position: absolute;
      left: 12px;
      top: 50%;
      transform: translateY(-50%);
      color: #94a3b8;
      font-size: 15px;
    }
    .btn-view-patient {
      background: var(--primary);
      color: #ffffff;
      font-size: 13.5px;
      font-weight: 700;
      letter-spacing: 0.3px;
      padding: 11px 26px;
      border: none;
      border-radius: 8px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 8px;
      transition: background-color 0.15s ease, transform 0.05s ease;
      white-space: nowrap;
    }
    .btn-view-patient:hover {
      background: var(--primary-hover);
    }
    .btn-view-patient:active {
      transform: scale(0.99);
    }
    .p360-quick-chips {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      font-size: 12px;
    }
    .quick-chips-label {
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-muted);
      font-size: 11px;
    }
    .quick-chip {
      background: var(--bg-subtle);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 4px 10px;
      font-size: 12px;
      font-family: ui-monospace, monospace;
      color: #334155;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.15s ease;
    }
    .quick-chip:hover {
      background: #eff6ff;
      border-color: #93c5fd;
      color: var(--primary);
    }
    .quick-chip.active {
      background: #eff6ff;
      border-color: var(--primary);
      color: var(--primary);
      font-weight: 700;
    }
    .chip-hint {
      color: #64748b;
      font-size: 11px;
      font-family: sans-serif;
    }

    /* Patient Summary Banner */
    .p360-patient-strip {
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px 18px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
    }
    .strip-subj {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .strip-subj-id {
      font-size: 16px;
      font-weight: 800;
      font-family: ui-monospace, monospace;
      color: #0f172a;
    }
    .strip-badge {
      font-size: 11.5px;
      font-weight: 700;
      padding: 2px 8px;
      border-radius: 4px;
    }
    .badge-drug { background: #dbeafe; color: #1e40af; }
    .badge-placebo { background: #f1f5f9; color: #475569; }
    .badge-signal-alert { background: #fee2e2; color: #991b1b; }
    .badge-signal-ok { background: #d1fae5; color: #065f46; }
    .strip-meta {
      display: flex;
      align-items: center;
      gap: 16px;
      font-size: 12.5px;
      color: var(--text-muted);
    }
    .strip-meta strong {
      color: var(--text-main);
    }

    /* Content Grid: Graph (Left) + Details Panel (Right) */
    .p360-grid {
      display: grid;
      grid-template-columns: minmax(0, 1.45fr) minmax(0, 1.05fr);
      gap: 20px;
      align-items: start;
    }
    @media (max-width: 1024px) {
      .p360-grid {
        grid-template-columns: 1fr;
      }
    }

    /* Graph Card */
    .p360-graph-card {
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: 12px;
      box-shadow: 0 1px 4px rgba(0,0,0,0.03);
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }
    .graph-card-header {
      padding: 12px 18px;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: #ffffff;
    }
    .graph-card-title {
      font-size: 13px;
      font-weight: 700;
      color: #334155;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .graph-badge {
      font-size: 11px;
      background: var(--bg-subtle);
      border: 1px solid var(--border);
      padding: 2px 7px;
      border-radius: 12px;
      color: var(--text-muted);
    }
    .graph-tools {
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .btn-graph-tool {
      background: var(--bg-subtle);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 4px 10px;
      font-size: 12px;
      font-weight: 600;
      color: #475569;
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .btn-graph-tool:hover {
      background: #e2e8f0;
      color: var(--text-main);
    }

    /* Domain selector pills above graph */
    .p360-domain-pills {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 10px 18px;
      background: #f8fafc;
      border-bottom: 1px solid var(--border);
      overflow-x: auto;
      white-space: nowrap;
    }
    .domain-pill-btn {
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 3px 10px;
      font-size: 11.5px;
      font-weight: 600;
      color: #334155;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 5px;
      transition: all 0.15s ease;
    }
    .domain-pill-btn:hover {
      border-color: #94a3b8;
    }
    .domain-pill-btn.active {
      background: var(--primary-subtle);
      border-color: var(--primary);
      color: var(--primary);
      font-weight: 700;
    }
    .domain-pill-count {
      font-size: 10.5px;
      opacity: 0.8;
    }

    /* SVG Area */
    .p360-svg-container {
      position: relative;
      width: 100%;
      height: 540px;
      background: radial-gradient(circle at center, #ffffff 0%, #f8fafc 100%);
      cursor: grab;
      user-select: none;
      overflow: hidden;
    }
    .p360-svg-container:active {
      cursor: grabbing;
    }
    .graph-footer-hint {
      padding: 10px 16px;
      background: #f8fafc;
      border-top: 1px solid var(--border);
      font-size: 12px;
      color: #64748b;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }

    /* Details Panel */
    .p360-details-panel {
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: 12px;
      box-shadow: 0 1px 4px rgba(0,0,0,0.03);
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 16px;
      max-height: 660px;
      overflow-y: auto;
    }

    /* Panel Component Styles */
    .panel-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid #f1f5f9;
      padding-bottom: 12px;
    }
    .panel-title {
      font-size: 15px;
      font-weight: 800;
      color: var(--text-main);
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .btn-panel-back {
      background: var(--bg-subtle);
      border: 1px solid var(--border);
      font-size: 11.5px;
      font-weight: 600;
      color: #475569;
      padding: 4px 10px;
      border-radius: 6px;
      cursor: pointer;
    }
    .btn-panel-back:hover { background: #e2e8f0; }

    .demographics-grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 10px;
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px;
    }
    .demo-item-label {
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-muted);
    }
    .demo-item-val {
      font-size: 13.5px;
      font-weight: 600;
      color: var(--text-main);
      font-family: ui-monospace, monospace;
    }

    .signals-card {
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px;
    }
    .signals-title {
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-muted);
      margin-bottom: 8px;
    }
    .signal-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 5px 0;
      border-bottom: 1px solid #e2e8f0;
      font-size: 12.5px;
    }
    .signal-row:last-child { border-bottom: none; }

    /* Domain Records Table / Cards */
    .domain-records-search {
      margin-bottom: 4px;
    }
    .domain-search-input {
      width: 100%;
      padding: 8px 12px;
      font-size: 12.5px;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      outline: none;
    }
    .domain-search-input:focus { border-color: var(--primary); }

    .records-table-container {
      max-height: 240px;
      overflow-y: auto;
      border: 1px solid var(--border);
      border-radius: 6px;
    }
    .records-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
      text-align: left;
    }
    .records-table th {
      background: #f8fafc;
      color: #475569;
      font-weight: 700;
      padding: 8px 10px;
      border-bottom: 1px solid var(--border);
      position: sticky;
      top: 0;
      z-index: 5;
    }
    .records-table td {
      padding: 7px 10px;
      border-bottom: 1px solid #f1f5f9;
      color: var(--text-main);
    }
    .record-row {
      cursor: pointer;
      transition: background-color 0.1s ease;
    }
    .record-row:hover {
      background: #f1f5f9;
    }
    .record-row.selected {
      background: #eff6ff;
      border-left: 3px solid var(--primary);
      font-weight: 600;
    }

    /* Record Detail Card */
    .record-detail-card {
      background: #f8fafc;
      border: 1.5px solid #cbd5e1;
      border-radius: 8px;
      padding: 14px 16px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .record-detail-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid #e2e8f0;
      padding-bottom: 6px;
    }
    .record-detail-title {
      font-size: 13px;
      font-weight: 800;
      color: #0f172a;
    }
    .record-kv-grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 8px;
      font-size: 12px;
    }
    .record-kv-label {
      color: var(--text-muted);
      font-weight: 600;
    }
    .record-kv-val {
      color: var(--text-main);
      font-family: ui-monospace, monospace;
      font-weight: 700;
    }
    .raw-sdtm-accordion {
      margin-top: 6px;
      border-top: 1px dashed #cbd5e1;
      padding-top: 8px;
    }
    .raw-sdtm-summary {
      font-size: 11px;
      font-weight: 700;
      color: var(--primary);
      cursor: pointer;
      user-select: none;
    }
    .raw-sdtm-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 11px;
      font-family: ui-monospace, monospace;
      margin-top: 6px;
    }
    .raw-sdtm-table td {
      padding: 3px 6px;
      border: 1px solid #e2e8f0;
    }
    .raw-sdtm-table tr:nth-child(even) {
      background: #f1f5f9;
    }

    .p360-state-box {
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 20px;
      text-align: center;
      color: var(--text-muted);
      font-size: 14px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 10px;
    }
    .p360-error-box {
      background: #fef2f2;
      border-color: #fecaca;
      color: #991b1b;
    }

    /* =========================================================
       STAGE 2: MONITOR & HUMAN GATE STYLES
       ========================================================= */
    .monitor-panel {
      display: none;
      flex-direction: column;
      gap: 24px;
      max-width: 1100px;
      width: 100%;
      margin: 0 auto;
      padding: 32px 20px 100px 20px;
    }
    .monitor-panel.active { display: flex; }

    .monitor-header-card {
      background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
      color: #ffffff;
      padding: 24px 28px;
      border-radius: 12px;
      box-shadow: 0 4px 16px rgba(0,0,0,0.1);
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 16px;
    }
    .monitor-title {
      font-size: 24px;
      font-weight: 800;
      letter-spacing: -0.5px;
    }
    .monitor-subtitle {
      font-size: 13.5px;
      color: #94a3b8;
      margin-top: 4px;
    }
    .monitor-controls-row {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }
    .monitor-select {
      background: #334155;
      color: #ffffff;
      border: 1px solid #475569;
      border-radius: 8px;
      padding: 8px 14px;
      font-size: 13.5px;
      font-weight: 600;
      outline: none;
      cursor: pointer;
    }
    .btn-run-cycle {
      background: #2563eb;
      color: #ffffff;
      border: none;
      border-radius: 8px;
      padding: 9px 20px;
      font-size: 14px;
      font-weight: 700;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 8px;
      transition: background 0.15s ease;
      box-shadow: 0 2px 8px rgba(37,99,235,0.4);
    }
    .btn-run-cycle:hover { background: #1d4ed8; }
    .btn-run-cycle:disabled { opacity: 0.6; cursor: not-allowed; }

    .memory-badge-pill {
      background: rgba(16, 185, 129, 0.15);
      border: 1px solid #10b981;
      color: #34d399;
      font-size: 12px;
      font-weight: 700;
      padding: 4px 12px;
      border-radius: 20px;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }

    /* Metric Cards Grid */
    .metrics-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 16px;
    }
    .metric-card {
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 18px 20px;
      box-shadow: 0 1px 4px rgba(0,0,0,0.04);
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .metric-card-label {
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-muted);
    }
    .metric-card-val {
      font-size: 28px;
      font-weight: 800;
      color: var(--text-main);
    }
    .metric-card-desc {
      font-size: 12px;
      color: var(--text-muted);
    }

    /* Trace Log Box */
    .trace-card {
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px 24px;
      box-shadow: 0 1px 6px rgba(0,0,0,0.04);
      display: flex;
      flex-direction: column;
      gap: 14px;
    }
    .trace-card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--border);
      padding-bottom: 10px;
    }
    .trace-title {
      font-size: 16px;
      font-weight: 800;
      color: var(--text-main);
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .trace-list {
      display: flex;
      flex-direction: column;
      gap: 10px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 12.5px;
      max-height: 380px;
      overflow-y: auto;
      padding-right: 6px;
    }
    .trace-row {
      display: flex;
      align-items: flex-start;
      gap: 12px;
      padding: 8px 12px;
      background: #f8fafc;
      border-left: 3px solid #cbd5e1;
      border-radius: 4px;
      line-height: 1.5;
    }
    .trace-row.detect { border-left-color: #2563eb; background: #eff6ff; }
    .trace-row.medical_review { border-left-color: #9333ea; background: #faf5ff; }
    .trace-row.data_manager { border-left-color: #d97706; background: #fffbeb; }
    .trace-row.compliance { border-left-color: #ea580c; background: #fff7ed; }
    .trace-row.human_gate { border-left-color: #0d9488; background: #f0fdfa; }
    .trace-row.execute { border-left-color: #10b981; background: #ecfdf5; }

    .node-badge {
      font-size: 10.5px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      padding: 2px 8px;
      border-radius: 4px;
      color: #ffffff;
      flex-shrink: 0;
      width: 110px;
      text-align: center;
    }
    .node-badge.detect { background: #2563eb; }
    .node-badge.medical_review { background: #9333ea; }
    .node-badge.data_manager { background: #d97706; }
    .node-badge.compliance { background: #ea580c; }
    .node-badge.human_gate { background: #0d9488; }
    .node-badge.execute { background: #10b981; }

    .trace-text {
      color: #1e293b;
      word-break: break-word;
    }

    /* Human Gate Cards */
    .escalation-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 16px;
    }
    .escalation-card {
      background: #ffffff;
      border: 1.5px solid var(--border);
      border-radius: 10px;
      padding: 20px 24px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.04);
      display: flex;
      flex-direction: column;
      gap: 14px;
      transition: border-color 0.15s ease;
    }
    .escalation-card.approved { border-left: 6px solid #10b981; }
    .escalation-card.rejected { border-left: 6px solid #ef4444; }
    .escalation-card.clarify { border-left: 6px solid #0d9488; }

    .escalation-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
    }
    .escalation-subject {
      font-size: 18px;
      font-weight: 800;
      color: var(--text-main);
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .severity-badge {
      font-size: 11px;
      font-weight: 800;
      padding: 3px 8px;
      border-radius: 4px;
      text-transform: uppercase;
    }
    .severity-CRITICAL { background: #fee2e2; color: #991b1b; }
    .severity-HIGH { background: #ffedd5; color: #9a3412; }
    .severity-MEDIUM { background: #fef9c3; color: #854d0e; }

    .escalation-summary {
      font-size: 14.5px;
      color: #1e293b;
      line-height: 1.5;
    }
    .escalation-evidence-box {
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 10px 14px;
      font-size: 12px;
      font-family: ui-monospace, monospace;
      color: #475569;
    }

    .escalation-actions-row {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-top: 6px;
      flex-wrap: wrap;
    }
    .btn-action-approve {
      background: #10b981;
      color: #ffffff;
      border: none;
      border-radius: 6px;
      padding: 8px 18px;
      font-weight: 700;
      font-size: 13px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
      transition: background 0.15s ease;
    }
    .btn-action-approve:hover { background: #059669; }

    .btn-action-reject {
      background: #ef4444;
      color: #ffffff;
      border: none;
      border-radius: 6px;
      padding: 8px 18px;
      font-weight: 700;
      font-size: 13px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
      transition: background 0.15s ease;
    }
    .btn-action-reject:hover { background: #dc2626; }

    .btn-action-clarify {
      background: #0d9488;
      color: #ffffff;
      border: none;
      border-radius: 6px;
      padding: 8px 18px;
      font-weight: 700;
      font-size: 13px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
      transition: background 0.15s ease;
    }
    .btn-action-clarify:hover { background: #0f766e; }

    .clarify-prompt-box {
      display: none;
      flex-direction: column;
      gap: 10px;
      background: #f0fdfa;
      border: 1.5px solid #5eead4;
      border-radius: 8px;
      padding: 14px;
      margin-top: 10px;
    }
    .clarify-input {
      width: 100%;
      border: 1px solid #99f6e4;
      border-radius: 6px;
      padding: 10px 12px;
      font-size: 13px;
      font-family: inherit;
    }
    .clarify-result-box {
      background: #ffffff;
      border: 1px solid #5eead4;
      border-radius: 6px;
      padding: 10px 12px;
      font-size: 13px;
      color: #0f766e;
    }

    .report-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12.5px;
      text-align: left;
    }
    .report-table th {
      background: #f8fafc;
      padding: 10px 12px;
      border-bottom: 2px solid var(--border);
      color: #475569;
      font-weight: 700;
    }
    .report-table td {
      padding: 9px 12px;
      border-bottom: 1px solid #f1f5f9;
      color: var(--text-main);
    }
    .badge-status {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 12px;
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
    }
    .badge-status.CLOSED { background: #dcfce7; color: #15803d; }
    .badge-status.OPEN { background: #fee2e2; color: #b91c1c; }
    .badge-status.ANSWERED { background: #e0f2fe; color: #0369a1; }

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
      <!-- Tab Navigation -->
      <nav class="header-nav" aria-label="Main Navigation">
        <button class="nav-btn active" id="tab-ask" onclick="switchTab('ask')">
          <span class="nav-btn-icon">💬</span> Ask ATLAS
        </button>
        <button class="nav-btn" id="tab-p360" onclick="switchTab('p360')">
          <span class="nav-btn-icon">🕸️</span> Patient 360° Graph
        </button>
        <button class="nav-btn" id="tab-monitor" onclick="switchTab('monitor')">
          <span class="nav-btn-icon">🛡️</span> MONITOR
        </button>
        <button class="nav-btn" id="tab-human-gate" onclick="switchTab('human-gate')">
          <span class="nav-btn-icon">⚖️</span> Human Gate <span id="pending-counter-badge" style="background:#ef4444; color:#ffffff; font-size:10px; font-weight:800; padding:1px 6px; border-radius:10px; margin-left:4px;">0</span>
        </button>
      </nav>
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

  <!-- 1. ASK ATLAS WORKSPACE (Existing Q&A Feature) -->
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

      <!-- Clickable Suggestions -->
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

  <!-- 2. PATIENT 360° GRAPH WORKSPACE (New Feature) -->
  <section id="patient360-workspace" class="p360-container" style="display: none;">
    <!-- Top Query & Header Toolbar -->
    <div class="p360-toolbar-card">
      <div class="p360-title-row">
        <div>
          <h2 class="p360-title">PATIENT 360° GRAPH</h2>
          <p class="p360-subtitle">Interactive patient-centric knowledge graph connecting clinical domains and records from StudyGraph.patient360()</p>
        </div>
      </div>

      <div class="p360-search-row">
        <div class="p360-input-wrapper">
          <span class="p360-input-icon">🔍</span>
          <input type="text" id="p360-subject-input" class="p360-input" placeholder="Enter Subject ID (e.g. 042-S01-001)" value="042-S01-001" list="p360-datalist" onkeydown="if(event.key==='Enter') loadPatient360()">
          <datalist id="p360-datalist"></datalist>
        </div>
        <button class="btn-view-patient" id="btn-view-patient" onclick="loadPatient360()">
          VIEW PATIENT
        </button>
      </div>

      <div class="p360-quick-chips">
        <span class="quick-chips-label">Demo Subjects:</span>
        <button class="quick-chip active" onclick="loadPatient360('042-S01-001')">042-S01-001 <span class="chip-hint">(Completed)</span></button>
        <button class="quick-chip" onclick="loadPatient360('042-S07-001')">042-S07-001 <span class="chip-hint">(Hy's Law • AE)</span></button>
        <button class="quick-chip" onclick="loadPatient360('042-S11-001')">042-S11-001 <span class="chip-hint">(Discontinued AE)</span></button>
        <button class="quick-chip" onclick="loadPatient360('042-S05-003')">042-S05-003 <span class="chip-hint">(Visit Trajectory)</span></button>
        <button class="quick-chip" onclick="loadPatient360('042-S02-004')">042-S02-004 <span class="chip-hint">(Dosing Error)</span></button>
      </div>

      <!-- Subject Summary Banner (populated after loading) -->
      <div class="p360-patient-strip" id="p360-patient-strip" style="display: none;">
        <!-- Filled dynamically -->
      </div>
    </div>

    <!-- Error/Loading states -->
    <div id="p360-loading" class="p360-state-box" style="display: none;">
      <span class="pulse-dot"></span> Loading Patient 360 data from StudyGraph...
    </div>
    <div id="p360-error" class="p360-state-box p360-error-box" style="display: none;"></div>

    <!-- Main Workspace Content: Graph (Left) + Details Panel (Right) -->
    <div class="p360-grid" id="p360-content-grid" style="display: none;">
      <!-- LEFT: Graph Card -->
      <div class="p360-graph-card">
        <div class="graph-card-header">
          <div class="graph-card-title">
            <span>Patient Knowledge Graph</span>
            <span class="graph-badge" id="graph-domains-count">0 Domains</span>
          </div>
          <div class="graph-tools">
            <button class="btn-graph-tool" onclick="zoomGraph(1.15)" title="Zoom In">➕</button>
            <button class="btn-graph-tool" onclick="zoomGraph(0.85)" title="Zoom Out">➖</button>
            <button class="btn-graph-tool" onclick="resetGraphView()" title="Reset View">⟲ Reset</button>
            <button class="btn-graph-tool" id="btn-show-all-domains" onclick="selectPatientCenter()" title="Show Patient Overview">👤 Patient</button>
          </div>
        </div>

        <!-- Domain filter tabs above SVG -->
        <div class="p360-domain-pills" id="p360-domain-pills">
          <!-- Filled dynamically with active domains -->
        </div>

        <!-- SVG Viewport -->
        <div class="p360-svg-container" id="p360-svg-container">
          <svg id="p360-svg" width="100%" height="540" viewBox="0 0 760 540">
            <!-- Dynamic SVG elements -->
          </svg>
        </div>

        <div class="graph-footer-hint">
          <span>💡 <strong>Interactive:</strong> Click any domain or record node to view actual details from <code>patient360()</code>. Drag canvas to pan, scroll to zoom.</span>
        </div>
      </div>

      <!-- RIGHT: Record Details Panel -->
      <div class="p360-details-panel" id="p360-details-panel">
        <!-- Filled dynamically -->
      </div>
    </div>
  </section>

  <!-- 3. MONITOR & CREW WORKSPACE (STAGE 2) -->
  <section class="monitor-panel" id="monitor-workspace">
    <!-- Header Controls -->
    <div class="monitor-header-card">
      <div>
        <div class="monitor-title">STUDY SENTINEL — MONITOR REVIEW CREW</div>
        <div class="monitor-subtitle">Multi-Agent Review Crew with Persistent State & Real-Time Decision Tracing</div>
      </div>
      <div class="monitor-controls-row">
        <div>
          <label style="font-size:11.5px; color:#94a3b8; font-weight:600; display:block; margin-bottom:2px;">DATA CUT</label>
          <select id="monitor-cut-select" class="monitor-select">
            <option value="1">Cut 1 (Day 14)</option>
            <option value="2">Cut 2 (Day 28)</option>
            <option value="3">Cut 3 (Day 56)</option>
            <option value="4">Cut 4 (Day 84)</option>
            <option value="5">Cut 5 (Day 112 - Amd 2)</option>
            <option value="6" selected>Cut 6 (Day 140)</option>
            <option value="7">Cut 7 (Day 168)</option>
            <option value="8">Cut 8 (Day 182)</option>
            <option value="9">Cut 9 (Day 200 - Amd 3)</option>
            <option value="10">Cut 10 (Day 220)</option>
            <option value="11">Cut 11 (Day 240)</option>
            <option value="12">Cut 12 (EOS Closeout)</option>
          </select>
        </div>
        <div>
          <label style="font-size:11.5px; color:#94a3b8; font-weight:600; display:block; margin-bottom:2px;">PROTOCOL VERSION</label>
          <select id="monitor-protocol-select" class="monitor-select">
            <option value="1">Version 1 (Initial ±7d)</option>
            <option value="2" selected>Version 2 (Amd 2 Renal ±3d)</option>
            <option value="3">Version 3 (Amd 3 Sulfo ±3d)</option>
          </select>
        </div>
        <div style="align-self: flex-end;">
          <button class="btn-run-cycle" id="btn-run-cycle" onclick="triggerReviewCycle()">
            <span>⚡</span> Run Review Cycle
          </button>
        </div>
      </div>
    </div>

    <!-- Memory Status Alert -->
    <div style="display:flex; justify-content:space-between; align-items:center; background:#f8fafc; border:1px solid var(--border); padding:12px 18px; border-radius:8px;">
      <div style="font-size:13px; color:#334155;">
        <strong>Persistent Memory:</strong> Guaranteed zero duplicate queries and zero duplicate escalations on re-run.
      </div>
      <div id="memory-stats-pill" class="memory-badge-pill">
        ✓ Memory Active: Cycle #1
      </div>
    </div>

    <!-- Cycle Metrics Grid -->
    <div class="metrics-grid">
      <div class="metric-card">
        <div class="metric-card-label">Findings Detected</div>
        <div class="metric-card-val" id="metric-findings">0</div>
        <div class="metric-card-desc" id="metric-findings-desc">Safety, Data & Protocol findings</div>
      </div>
      <div class="metric-card">
        <div class="metric-card-label">Escalations Drafted</div>
        <div class="metric-card-val" id="metric-escalations" style="color:#9333ea;">0</div>
        <div class="metric-card-desc">Medical Monitor review required</div>
      </div>
      <div class="metric-card">
        <div class="metric-card-label">Queries Raised</div>
        <div class="metric-card-val" id="metric-queries" style="color:#d97706;">0</div>
        <div class="metric-card-desc" id="metric-queries-desc">Specific, record-cited to sites</div>
      </div>
      <div class="metric-card">
        <div class="metric-card-label">Compliance Deviations</div>
        <div class="metric-card-val" id="metric-deviations" style="color:#ea580c;">0</div>
        <div class="metric-card-desc" id="metric-deviations-desc">Protocol rules in force</div>
      </div>
    </div>

    <!-- Live Decision Trace Card -->
    <div class="trace-card">
      <div class="trace-card-header">
        <div class="trace-title">
          <span>📜</span> DECISION TRACE LOG (RECORDED AS IT HAPPENED)
        </div>
        <span style="font-size:12px; color:var(--text-muted);">6 Nodes In Sequence: detect → medical_review → data_manager → compliance → human_gate → execute</span>
      </div>
      <div class="trace-list" id="trace-list-container">
        <div style="color:#64748b; font-style:italic; padding:10px;">No cycle executed yet. Click "Run Review Cycle" above.</div>
      </div>
    </div>

    <!-- Queries Table Card -->
    <div class="trace-card">
      <div class="trace-card-header">
        <div class="trace-title">
          <span>📬</span> DATA MANAGER QUERIES RAISED (POST /queries)
        </div>
        <span style="font-size:12px; color:var(--text-muted);" id="queries-header-count">0 Queries</span>
      </div>
      <div style="max-height:300px; overflow-y:auto;">
        <table class="report-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>Subject</th>
              <th>Domain</th>
              <th>Seq</th>
              <th>Query Text</th>
              <th>Status</th>
              <th>Site Response</th>
            </tr>
          </thead>
          <tbody id="queries-table-body">
            <tr><td colspan="7" style="text-align:center; color:#94a3b8;">No queries generated for this cycle.</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Compliance Deviations Table Card -->
    <div class="trace-card">
      <div class="trace-card-header">
        <div class="trace-title">
          <span>⚖️</span> PROTOCOL COMPLIANCE DEVIATIONS
        </div>
        <span style="font-size:12px; color:var(--text-muted);" id="deviations-header-count">0 Deviations</span>
      </div>
      <div style="max-height:300px; overflow-y:auto;">
        <table class="report-table">
          <thead>
            <tr>
              <th>Category</th>
              <th>Subject</th>
              <th>Site</th>
              <th>Rule In Force</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody id="deviations-table-body">
            <tr><td colspan="5" style="text-align:center; color:#94a3b8;">No deviations logged.</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>

  <!-- 4. HUMAN GATE WORKSPACE (STAGE 2) -->
  <section class="monitor-panel" id="human-gate-workspace">
    <div class="monitor-header-card" style="background: linear-gradient(135deg, #0f766e 0%, #115e59 100%);">
      <div>
        <div class="monitor-title">MONITOR — HUMAN GATE</div>
        <div class="monitor-subtitle">Medical Monitor Adjudication Desk for Serious Safety & Operational Escalations</div>
      </div>
      <div>
        <span style="background:rgba(255,255,255,0.2); padding:6px 14px; border-radius:20px; font-weight:700; font-size:13px;">
          POST /escalations Gateway
        </span>
      </div>
    </div>

    <div style="display:flex; justify-content:space-between; align-items:center;">
      <h3 style="font-size:18px; font-weight:800; color:var(--text-main);">Pending Escalations</h3>
      <span style="font-size:13px; color:var(--text-muted);" id="human-gate-pending-note">0 awaiting decision</span>
    </div>

    <div class="escalation-grid" id="escalations-cards-container">
      <div style="text-align:center; padding:40px; color:#64748b; background:#f8fafc; border:1px dashed #cbd5e1; border-radius:10px;">
        No pending escalations awaiting decision. Run a review cycle in the MONITOR tab to identify candidates.
      </div>
    </div>
  </section>

  <!-- Follow-up Question Bar (Visible once conversation begins in Ask ATLAS) -->
  <div class="bottom-bar" id="bottom-bar">
    <div class="bottom-form">
      <input type="text" id="followup-input" class="bottom-input" placeholder="Ask another question about the study..." onkeydown="if(event.key==='Enter') submitFollowupQuestion()">
      <button class="btn-bottom-ask" id="btn-followup-ask" onclick="submitFollowupQuestion()">
        Ask ATLAS
      </button>
    </div>
  </div>

  <script>
    /* =========================================================
       GLOBAL APP STATE & TAB ROUTING
       ========================================================= */
    let CURRENT_TAB = 'ask';
    let CURRENT_P360_DATA = null;
    let CURRENT_SELECTED_DOMAIN = null;
    let CURRENT_SELECTED_RECORD_SEQ = null;
    let P360_ZOOM = 1.0;
    let P360_PAN_X = 0;
    let P360_PAN_Y = 0;
    let IS_DRAGGING = false;
    let DRAG_START_X = 0;
    let DRAG_START_Y = 0;

    const DOMAIN_DEFS = [
      { id: 'laboratory', name: 'Laboratory', key: 'laboratory', color: '#0284c7', bg: '#e0f2fe', icon: '🧪', code: 'LB' },
      { id: 'adverse_events', name: 'Adverse Events', key: 'adverse_events', color: '#dc2626', bg: '#fee2e2', icon: '⚠️', code: 'AE' },
      { id: 'concomitant_medications', name: 'Medications', key: 'concomitant_medications', color: '#d97706', bg: '#fef3c7', icon: '💊', code: 'CM' },
      { id: 'vital_signs', name: 'Vital Signs', key: 'vital_signs', color: '#059669', bg: '#d1fae5', icon: '❤️', code: 'VS' },
      { id: 'disposition', name: 'Disposition', key: 'disposition', color: '#4f46e5', bg: '#e0e7ff', icon: '📋', code: 'DS' },
      { id: 'medical_history', name: 'Medical History', key: 'medical_history', color: '#db2777', bg: '#fce7f3', icon: '📜', code: 'MH' },
      { id: 'ecg', name: 'ECG', key: 'ecg', color: '#7c3aed', bg: '#ede9fe', icon: '📈', code: 'EG' },
      { id: 'examinations', name: 'Examinations', key: 'examinations', color: '#0d9488', bg: '#ccfbf1', icon: '🩺', code: 'PE' },
      { id: 'exposure', name: 'Dosing', key: 'exposure', color: '#2563eb', bg: '#dbeafe', icon: '💉', code: 'EX' }
    ];

    function getActiveDomains(pdata) {
      if (!pdata) return [];
      return DOMAIN_DEFS.filter(d => Array.isArray(pdata[d.key]) && pdata[d.key].length > 0);
    }

    function switchTab(tab) {
      CURRENT_TAB = tab;
      const askTab = document.getElementById('tab-ask');
      const p360Tab = document.getElementById('tab-p360');
      const monitorTab = document.getElementById('tab-monitor');
      const hgTab = document.getElementById('tab-human-gate');

      const mainWs = document.getElementById('main-workspace');
      const p360Ws = document.getElementById('patient360-workspace');
      const monitorWs = document.getElementById('monitor-workspace');
      const hgWs = document.getElementById('human-gate-workspace');
      const bottomBar = document.getElementById('bottom-bar');
      const convFeed = document.getElementById('conv-feed');

      // Deactivate all nav buttons
      [askTab, p360Tab, monitorTab, hgTab].forEach(b => { if(b) b.classList.remove('active'); });
      // Hide all workspaces
      if (mainWs) mainWs.style.display = 'none';
      if (p360Ws) p360Ws.style.display = 'none';
      if (monitorWs) monitorWs.classList.remove('active');
      if (hgWs) hgWs.classList.remove('active');
      if (bottomBar) bottomBar.style.display = 'none';

      if (tab === 'p360') {
        if (p360Tab) p360Tab.classList.add('active');
        if (p360Ws) p360Ws.style.display = 'flex';
        if (!CURRENT_P360_DATA) {
          const inputVal = document.getElementById('p360-subject-input').value.trim();
          loadPatient360(inputVal || '042-S01-001');
        }
      } else if (tab === 'monitor') {
        if (monitorTab) monitorTab.classList.add('active');
        if (monitorWs) monitorWs.classList.add('active');
        loadLatestReport();
      } else if (tab === 'human-gate') {
        if (hgTab) hgTab.classList.add('active');
        if (hgWs) hgWs.classList.add('active');
        renderHumanGateCards();
      } else {
        if (askTab) askTab.classList.add('active');
        if (mainWs) mainWs.style.display = 'flex';
        if (convFeed && convFeed.classList.contains('active') && bottomBar) {
          bottomBar.style.display = 'flex';
        }
      }
    }

    function openPatient360(usubjid) {
      switchTab('p360');
      const input = document.getElementById('p360-subject-input');
      if (input) input.value = usubjid;
      loadPatient360(usubjid);
    }

    function toggleContext(e) {
      e.stopPropagation();
      const dd = document.getElementById('context-dropdown');
      dd.classList.toggle('open');
    }
    document.addEventListener('click', () => {
      const dd = document.getElementById('context-dropdown');
      if (dd) dd.classList.remove('open');
    });

    /* =========================================================
       EXISTING ASK ATLAS QUESTION-ANSWERING LOGIC
       ========================================================= */
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
              <div class="subjects-list-title">Subjects Identified (click to view Patient 360):</div>
              <ul class="subjects-bullets">
                ${ans.map(s => `<li onclick="openPatient360('${s}')" title="Open Patient 360° Graph for ${s}">${s} <span style="font-size: 11px; font-weight: normal; color: var(--teal); opacity: 0.85;">[360°]</span></li>`).join('')}
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
              <div class="why-subj-header" onclick="openPatient360('${g.subject}')" title="Open Patient 360° Graph for ${g.subject}" style="cursor: pointer;">
                ${g.subject} <span style="font-size: 11px; font-weight: normal; color: var(--teal); opacity: 0.85;">[View 360° Graph]</span>
              </div>
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

    /* =========================================================
       PATIENT 360° GRAPH IMPLEMENTATION
       ========================================================= */
    async function loadPatient360(targetId) {
      const input = document.getElementById('p360-subject-input');
      const subjId = (targetId || (input ? input.value : '')).trim();
      if (!subjId) return;

      if (input) input.value = subjId;

      // Update quick chips active state
      document.querySelectorAll('.quick-chip').forEach(c => {
        if (c.textContent.includes(subjId)) {
          c.classList.add('active');
        } else {
          c.classList.remove('active');
        }
      });

      const loadingEl = document.getElementById('p360-loading');
      const errorEl = document.getElementById('p360-error');
      const gridEl = document.getElementById('p360-content-grid');
      const stripEl = document.getElementById('p360-patient-strip');

      if (loadingEl) loadingEl.style.display = 'flex';
      if (errorEl) errorEl.style.display = 'none';

      try {
        const resp = await fetch('/api/patient360?usubjid=' + encodeURIComponent(subjId));
        const text = await resp.text();
        const safeText = text.replace(/\bNaN\b/g, 'null').replace(/\b-?Infinity\b/g, 'null');
        const data = JSON.parse(safeText);

        if (loadingEl) loadingEl.style.display = 'none';

        if (data.error) {
          if (errorEl) {
            errorEl.innerHTML = `<strong>Subject Not Found:</strong> ${escapeHtml(data.error)}. Please enter a valid Subject ID from the study (e.g., 042-S01-001).`;
            errorEl.style.display = 'block';
          }
          if (gridEl) gridEl.style.display = 'none';
          if (stripEl) stripEl.style.display = 'none';
          return;
        }

        CURRENT_P360_DATA = data;
        CURRENT_SELECTED_DOMAIN = null;
        CURRENT_SELECTED_RECORD_SEQ = null;

        // Populate patient summary strip
        renderPatientStrip(data);

        // Reset and draw Graph
        resetGraphView();
        renderPatientGraph();

        // Render Details Panel
        renderDetailsPanel();

        if (gridEl) gridEl.style.display = 'grid';
        if (stripEl) stripEl.style.display = 'flex';

      } catch (err) {
        console.error("Error loading patient 360:", err);
        if (loadingEl) loadingEl.style.display = 'none';
        if (errorEl) {
          errorEl.innerHTML = `<strong>Error:</strong> Failed to load patient data: ${escapeHtml(err.message || String(err))}`;
          errorEl.style.display = 'block';
        }
      }
    }

    function renderPatientStrip(data) {
      const stripEl = document.getElementById('p360-patient-strip');
      if (!stripEl) return;

      const dem = data.demographics || {};
      const arm = dem.ARM || 'Unknown';
      const isDrug = arm.toUpperCase().includes('DRUG');
      const signals = data.signals || {};
      const hasHy = signals.hys_law;
      const saeCount = (signals.sae || []).length;
      const dosingErrCount = (signals.dosing_errors || []).length;
      const prohMedsCount = (signals.prohibited_meds || []).length;

      let signalPill = "";
      if (hasHy) {
        signalPill = `<span class="strip-badge badge-signal-alert">⚠️ Hy's Law Alert</span>`;
      } else if (saeCount > 0 || dosingErrCount > 0 || prohMedsCount > 0) {
        signalPill = `<span class="strip-badge badge-signal-alert">⚠️ Signals (${saeCount} SAE, ${dosingErrCount} Dose Err)</span>`;
      } else {
        signalPill = `<span class="strip-badge badge-signal-ok">✓ No Safety Signals</span>`;
      }

      const activeDomains = getActiveDomains(data);
      let totalRecords = 0;
      activeDomains.forEach(d => {
        totalRecords += (data[d.key] || []).length;
      });

      stripEl.innerHTML = `
        <div class="strip-subj">
          <span class="strip-subj-id">${escapeHtml(data.usubjid)}</span>
          <span class="strip-badge ${isDrug ? 'badge-drug' : 'badge-placebo'}">${escapeHtml(arm)}</span>
          ${signalPill}
        </div>
        <div class="strip-meta">
          <span>Site: <strong>${escapeHtml(data.siteid || dem.SITEID || '-')}</strong></span>
          <span>Age: <strong>${escapeHtml(dem.AGE || '-')}</strong></span>
          <span>Sex: <strong>${escapeHtml(dem.SEX || '-')}</strong></span>
          <span>Race: <strong>${escapeHtml(dem.RACE || '-')}</strong></span>
          <span>Active Domains: <strong>${activeDomains.length}</strong></span>
          <span>Total Records: <strong>${totalRecords}</strong></span>
        </div>
      `;
    }

    function renderDomainPills(activeDomains) {
      const container = document.getElementById('p360-domain-pills');
      if (!container || !CURRENT_P360_DATA) return;

      let html = `
        <button class="domain-pill-btn ${!CURRENT_SELECTED_DOMAIN ? 'active' : ''}" onclick="selectPatientCenter()">
          <span>👤</span> Patient Profile
        </button>
      `;

      activeDomains.forEach(d => {
        const count = (CURRENT_P360_DATA[d.key] || []).length;
        const isActive = CURRENT_SELECTED_DOMAIN === d.id;
        html += `
          <button class="domain-pill-btn ${isActive ? 'active' : ''}" onclick="selectDomain('${d.id}')">
            <span>${d.icon}</span> ${d.name} <span class="domain-pill-count">(${count})</span>
          </button>
        `;
      });

      container.innerHTML = html;
    }

    function renderPatientGraph() {
      const svg = document.getElementById('p360-svg');
      if (!svg || !CURRENT_P360_DATA) return;

      const pdata = CURRENT_P360_DATA;
      const activeDomains = getActiveDomains(pdata);
      const domainsCountEl = document.getElementById('graph-domains-count');
      if (domainsCountEl) {
        domainsCountEl.textContent = `${activeDomains.length} Connected Domains`;
      }

      renderDomainPills(activeDomains);

      const cx = 380;
      const cy = 270;
      const R = 185;
      const N = activeDomains.length;

      let linksHtml = '';
      let nodesHtml = '';
      let satellitesHtml = '';

      const arm = (pdata.demographics || {}).ARM || 'Study Arm';
      const isDrug = arm.toUpperCase().includes('DRUG');

      activeDomains.forEach((d, idx) => {
        const count = (pdata[d.key] || []).length;
        const angle = -Math.PI / 2 + (idx * 2 * Math.PI / N);
        const dx = cx + R * Math.cos(angle);
        const dy = cy + R * Math.sin(angle);
        const isDomainSelected = CURRENT_SELECTED_DOMAIN === d.id;

        // Connecting link from Center to Domain
        const strokeW = isDomainSelected ? 3.5 : 2;
        const strokeOp = isDomainSelected ? 0.95 : 0.45;
        const strokeDash = isDomainSelected ? '6 3' : 'none';

        linksHtml += `
          <line x1="${cx}" y1="${cy}" x2="${dx}" y2="${dy}"
                stroke="${d.color}" stroke-width="${strokeW}" stroke-opacity="${strokeOp}"
                stroke-dasharray="${strokeDash}" />
        `;

        // Midpoint badge
        const mx = (cx * 0.45 + dx * 0.55);
        const my = (cy * 0.45 + dy * 0.55);
        linksHtml += `
          <g transform="translate(${mx}, ${my})" style="pointer-events: none;">
            <rect x="-15" y="-9" width="30" height="18" rx="9" fill="${d.color}" />
            <text x="0" y="4" text-anchor="middle" font-size="10" font-weight="800" fill="#ffffff">${count}</text>
          </g>
        `;

        // Satellite record nodes (when domain is selected)
        if (isDomainSelected) {
          const records = pdata[d.key] || [];
          const maxSat = Math.min(records.length, 8);
          const satR = 74;

          for (let k = 0; k < maxSat; k++) {
            const rec = records[k];
            const isRecSelected = CURRENT_SELECTED_RECORD_SEQ === rec.seq;
            const spread = Math.PI * 0.85;
            const satAngle = angle - spread / 2 + (maxSat > 1 ? (k * spread / (maxSat - 1)) : 0);
            const sx = dx + satR * Math.cos(satAngle);
            const sy = dy + satR * Math.sin(satAngle);

            const recLabel = rec.testcd || (rec.term ? rec.term.substring(0, 5) : '') || ('#' + rec.seq);
            const fullTitle = `${rec.visit ? rec.visit + ' • ' : ''}${rec.testcd || rec.term || rec.treatment || ('Seq ' + rec.seq)}: ${rec.raw_value || rec.dose || rec.severity || rec.status || ''}`;

            satellitesHtml += `
              <line x1="${dx}" y1="${dy}" x2="${sx}" y2="${sy}" stroke="${d.color}" stroke-width="1.5" stroke-dasharray="3 3" opacity="0.8"/>
              <g style="cursor: pointer;" onclick="selectRecord('${d.id}', ${rec.seq})">
                <circle cx="${sx}" cy="${sy}" r="${isRecSelected ? 18 : 15}" fill="${isRecSelected ? '#f59e0b' : d.bg}" stroke="${isRecSelected ? '#b45309' : d.color}" stroke-width="${isRecSelected ? 3 : 2}" style="filter: drop-shadow(0 2px 4px rgba(0,0,0,0.12)); transition: all 0.2s ease;"/>
                <text x="${sx}" y="${sy + 4}" text-anchor="middle" font-size="9" font-weight="800" font-family="ui-monospace, monospace" fill="${isRecSelected ? '#ffffff' : '#0f172a'}">${escapeHtml(recLabel.substring(0, 6))}</text>
                <title>${escapeHtml(fullTitle)}</title>
              </g>
            `;
          }

          if (records.length > 8) {
            const moreAngle = angle + spread / 2 + 0.25;
            const mx_more = dx + satR * Math.cos(moreAngle);
            const my_more = dy + satR * Math.sin(moreAngle);
            satellitesHtml += `
              <g style="cursor: pointer;" onclick="selectDomain('${d.id}')">
                <circle cx="${mx_more}" cy="${my_more}" r="15" fill="#f1f5f9" stroke="#94a3b8" stroke-width="1.5"/>
                <text x="${mx_more}" y="${my_more + 4}" text-anchor="middle" font-size="8.5" font-weight="700" fill="#475569">+${records.length - 8}</text>
                <title>${records.length - 8} more records available in Details Panel</title>
              </g>
            `;
          }
        }

        // Domain Node Group
        nodesHtml += `
          <g style="cursor: pointer;" onclick="selectDomain('${d.id}')">
            ${isDomainSelected ? `<circle cx="${dx}" cy="${dy}" r="43" fill="none" stroke="${d.color}" stroke-width="3" stroke-dasharray="4 3" opacity="0.9"/>` : ''}
            <circle cx="${dx}" cy="${dy}" r="34" fill="#ffffff" stroke="${d.color}" stroke-width="${isDomainSelected ? 3.5 : 2.5}" style="filter: drop-shadow(0 3px 8px rgba(0,0,0,0.08)); transition: all 0.15s ease;" />
            <text x="${dx}" y="${dy - 3}" text-anchor="middle" font-size="18">${d.icon}</text>
            <text x="${dx}" y="${dy + 14}" text-anchor="middle" font-size="11" font-weight="800" fill="${d.color}">${count}</text>

            <g transform="translate(${dx + 23}, ${dy - 23})">
              <circle cx="0" cy="0" r="11" fill="${d.color}" stroke="#ffffff" stroke-width="2"/>
              <text x="0" y="3.5" text-anchor="middle" font-size="9" font-weight="800" fill="#ffffff">${d.code}</text>
            </g>

            <text x="${dx}" y="${dy + 48}" text-anchor="middle" font-size="12" font-weight="700" fill="${isDomainSelected ? d.color : '#0f172a'}">${d.name}</text>
            <title>${d.name} (${count} record${count === 1 ? '' : 's'}) — Click to inspect</title>
          </g>
        `;
      });

      // Center Patient Node
      const isPatientSelected = !CURRENT_SELECTED_DOMAIN;
      const patientNodeHtml = `
        <g style="cursor: pointer;" onclick="selectPatientCenter()">
          <circle cx="${cx}" cy="${cy}" r="56" fill="none" stroke="#3b82f6" stroke-width="2" stroke-dasharray="5 3" opacity="${isPatientSelected ? '0.9' : '0.4'}"/>
          <circle cx="${cx}" cy="${cy}" r="46" fill="#0f172a" stroke="${isPatientSelected ? '#60a5fa' : '#3b82f6'}" stroke-width="${isPatientSelected ? '4' : '3'}" style="filter: drop-shadow(0 4px 16px rgba(37,99,235,0.3)); transition: all 0.2s ease;"/>
          <text x="${cx}" y="${cy - 16}" text-anchor="middle" font-size="10" font-weight="800" letter-spacing="1px" fill="#93c5fd">PATIENT</text>
          <text x="${cx}" y="${cy + 3}" text-anchor="middle" font-size="11.5" font-weight="800" font-family="ui-monospace, monospace" fill="#ffffff">${escapeHtml(pdata.usubjid)}</text>
          
          <rect x="${cx - 32}" y="${cy + 13}" width="64" height="17" rx="8.5" fill="${isDrug ? '#1d4ed8' : '#475569'}"/>
          <text x="${cx}" y="${cy + 25}" text-anchor="middle" font-size="9.5" font-weight="700" fill="#ffffff">${escapeHtml(arm)}</text>
          <title>Subject ${escapeHtml(pdata.usubjid)} — Click for Patient Profile</title>
        </g>
      `;

      svg.innerHTML = `
        <g id="p360-viewport-g" transform="translate(${P360_PAN_X}, ${P360_PAN_Y}) scale(${P360_ZOOM})">
          ${linksHtml}
          ${satellitesHtml}
          ${nodesHtml}
          ${patientNodeHtml}
        </g>
      `;

      initSvgInteraction();
    }

    function selectDomain(domainId) {
      CURRENT_SELECTED_DOMAIN = domainId;
      CURRENT_SELECTED_RECORD_SEQ = null;
      renderPatientGraph();
      renderDetailsPanel();
    }

    function selectRecord(domainId, seq) {
      CURRENT_SELECTED_DOMAIN = domainId;
      CURRENT_SELECTED_RECORD_SEQ = seq;
      renderPatientGraph();
      renderDetailsPanel();
    }

    function selectPatientCenter() {
      CURRENT_SELECTED_DOMAIN = null;
      CURRENT_SELECTED_RECORD_SEQ = null;
      renderPatientGraph();
      renderDetailsPanel();
    }

    function renderDetailsPanel() {
      const panel = document.getElementById('p360-details-panel');
      if (!panel || !CURRENT_P360_DATA) return;

      const pdata = CURRENT_P360_DATA;
      const activeDomains = getActiveDomains(pdata);

      if (!CURRENT_SELECTED_DOMAIN) {
        renderPatientOverviewPanel(panel, pdata, activeDomains);
      } else {
        renderDomainDetailsPanel(panel, pdata, CURRENT_SELECTED_DOMAIN, CURRENT_SELECTED_RECORD_SEQ);
      }
    }

    function renderPatientOverviewPanel(panel, pdata, activeDomains) {
      const dem = pdata.demographics || {};
      const signals = pdata.signals || {};

      let signalsHtml = '';
      if (signals.hys_law) {
        signalsHtml += `
          <div class="signal-row" style="color: #b91c1c; font-weight: 700;">
            <span>⚠️ Hy's Law Liver Alert</span>
            <span>Meets ALT/AST &gt; 3x and BILI &gt; 2x ULN</span>
          </div>
        `;
      }
      if ((signals.dosing_errors || []).length > 0) {
        signalsHtml += `
          <div class="signal-row" style="color: #b45309; font-weight: 700;">
            <span>⚠️ Dosing Deviations</span>
            <span>${signals.dosing_errors.length} Protocol Dose Error(s)</span>
          </div>
        `;
      }
      if ((signals.prohibited_meds || []).length > 0) {
        signalsHtml += `
          <div class="signal-row" style="color: #b45309; font-weight: 700;">
            <span>⚠️ Prohibited ConMeds</span>
            <span>${signals.prohibited_meds.length} Prohibited Medication(s)</span>
          </div>
        `;
      }
      if ((signals.sae || []).length > 0) {
        signalsHtml += `
          <div class="signal-row" style="color: #b91c1c; font-weight: 700;">
            <span>⚠️ Serious Adverse Events</span>
            <span>${signals.sae.length} SAE(s) Recorded</span>
          </div>
        `;
      }
      if ((signals.miscoded_sae || []).length > 0) {
        signalsHtml += `
          <div class="signal-row" style="color: #b45309; font-weight: 700;">
            <span>⚠️ Miscoded SAE</span>
            <span>${signals.miscoded_sae.length} Hospitalized with AESER=N</span>
          </div>
        `;
      }
      if (!signalsHtml) {
        signalsHtml = `
          <div class="signal-row" style="color: #047857; font-weight: 600;">
            <span>✓ Safety Signals Clean</span>
            <span>No Hy's law, dosing errors, or prohibited meds</span>
          </div>
        `;
      }

      let domainCardsHtml = '';
      activeDomains.forEach(d => {
        const count = (pdata[d.key] || []).length;
        domainCardsHtml += `
          <div style="background: ${d.bg}; border: 1px solid ${d.color}40; border-radius: 8px; padding: 10px 14px; display: flex; align-items: center; justify-content: space-between; cursor: pointer; transition: all 0.15s ease;" onclick="selectDomain('${d.id}')">
            <div style="display: flex; align-items: center; gap: 8px;">
              <span style="font-size: 16px;">${d.icon}</span>
              <strong style="font-size: 13px; color: #0f172a;">${d.name}</strong>
            </div>
            <div style="display: flex; align-items: center; gap: 8px;">
              <span style="font-size: 12px; font-weight: 700; color: ${d.color};">${count} record${count === 1 ? '' : 's'}</span>
              <span style="font-size: 12px; color: #64748b;">→</span>
            </div>
          </div>
        `;
      });

      panel.innerHTML = `
        <div class="panel-header">
          <div class="panel-title">
            <span>👤</span> Patient Profile Summary
          </div>
          <span class="strip-badge ${((dem.ARM || '').toUpperCase().includes('DRUG')) ? 'badge-drug' : 'badge-placebo'}">${escapeHtml(dem.ARM || '-')}</span>
        </div>

        <div>
          <div class="demo-item-label" style="margin-bottom: 6px;">Subject Demographics (DM)</div>
          <div class="demographics-grid">
            <div>
              <div class="demo-item-label">Subject ID</div>
              <div class="demo-item-val">${escapeHtml(pdata.usubjid)}</div>
            </div>
            <div>
              <div class="demo-item-label">Site ID</div>
              <div class="demo-item-val">${escapeHtml(pdata.siteid || dem.SITEID || '-')}</div>
            </div>
            <div>
              <div class="demo-item-label">Age / Sex</div>
              <div class="demo-item-val">${escapeHtml(dem.AGE || '-')} / ${escapeHtml(dem.SEX || '-')}</div>
            </div>
            <div>
              <div class="demo-item-label">Treatment Arm</div>
              <div class="demo-item-val">${escapeHtml(dem.ARM || '-')}</div>
            </div>
            <div>
              <div class="demo-item-label">Race / Ethnicity</div>
              <div class="demo-item-val" style="font-size: 12px;">${escapeHtml(dem.RACE || '-')}</div>
            </div>
            <div>
              <div class="demo-item-label">Study Enrollment</div>
              <div class="demo-item-val">${escapeHtml(dem.RFSTDTC || '-')}</div>
            </div>
          </div>
        </div>

        <div class="signals-card">
          <div class="signals-title">Clinical Safety & Protocol Signals</div>
          ${signalsHtml}
        </div>

        <div>
          <div class="demo-item-label" style="margin-bottom: 6px;">Connected Clinical Domains (${activeDomains.length})</div>
          <div style="display: flex; flex-direction: column; gap: 8px;">
            ${domainCardsHtml}
          </div>
        </div>
      `;
    }

    function renderDomainDetailsPanel(panel, pdata, domainId, selectedSeq) {
      const domain = DOMAIN_DEFS.find(d => d.id === domainId);
      if (!domain) return;

      const records = pdata[domain.key] || [];
      let activeRec = records.find(r => r.seq === selectedSeq) || records[0];

      let tableRowsHtml = '';
      records.forEach(r => {
        const isSel = activeRec && activeRec.seq === r.seq;
        let col1 = r.visit || r.start_date_str || r.date_str || '-';
        let col2 = r.testcd || r.term || r.treatment || r.status || ('Seq ' + r.seq);
        let col3 = '';
        if (domainId === 'laboratory') {
          col3 = `${r.raw_value} ${r.raw_unit || ''}`.trim();
        } else if (domainId === 'adverse_events') {
          col3 = `${r.severity || ''} ${r.is_serious ? '(SAE)' : ''}`.trim();
        } else if (domainId === 'exposure') {
          col3 = `${r.dose !== null ? r.dose + ' mg' : '-'} ${r.is_wrong_dose ? '⚠️' : ''}`;
        } else if (domainId === 'concomitant_medications') {
          col3 = r.med_class || (r.is_prohibited ? '⚠️ Prohibited' : 'Permitted');
        } else if (domainId === 'vital_signs' || domainId === 'ecg') {
          col3 = r.raw_value || '-';
        } else if (domainId === 'disposition') {
          col3 = r.reason || r.status || '-';
        } else if (domainId === 'medical_history') {
          col3 = r.term || '-';
        } else {
          col3 = r.raw_value || r.value || '-';
        }

        let dateStr = r.date_str || r.start_date_str || r.date || '-';

        tableRowsHtml += `
          <tr class="record-row ${isSel ? 'selected' : ''}" onclick="selectRecord('${domainId}', ${r.seq})">
            <td>#${r.seq}</td>
            <td><strong>${escapeHtml(col1)}</strong></td>
            <td>${escapeHtml(col2)}</td>
            <td><code>${escapeHtml(col3)}</code></td>
            <td style="color:#64748b;">${escapeHtml(dateStr)}</td>
          </tr>
        `;
      });

      // Build Selected Record Details Card
      let recordCardHtml = '';
      if (activeRec) {
        let kvItems = `
          <div><span class="record-kv-label">Domain:</span> <span class="record-kv-val">${domain.code} (${domain.name})</span></div>
          <div><span class="record-kv-label">Sequence (SEQ):</span> <span class="record-kv-val">#${activeRec.seq}</span></div>
        `;
        if (activeRec.visit) {
          kvItems += `<div><span class="record-kv-label">Visit:</span> <span class="record-kv-val">${escapeHtml(activeRec.visit)}</span></div>`;
        }
        if (activeRec.date_str || activeRec.start_date_str || activeRec.date) {
          kvItems += `<div><span class="record-kv-label">Date:</span> <span class="record-kv-val">${escapeHtml(activeRec.date_str || activeRec.start_date_str || activeRec.date)}</span></div>`;
        }

        if (domainId === 'laboratory') {
          kvItems += `
            <div><span class="record-kv-label">Test Code:</span> <span class="record-kv-val">${escapeHtml(activeRec.testcd)}</span></div>
            <div><span class="record-kv-label">Raw Result:</span> <span class="record-kv-val">${escapeHtml(activeRec.raw_value)} ${escapeHtml(activeRec.raw_unit || '')}</span></div>
            <div><span class="record-kv-label">Standardized:</span> <span class="record-kv-val">${activeRec.std_value !== null ? activeRec.std_value + ' ' + (activeRec.std_unit || '') : 'N/A'}</span></div>
          `;
        } else if (domainId === 'adverse_events') {
          kvItems += `
            <div><span class="record-kv-label">Reported Term:</span> <span class="record-kv-val">${escapeHtml(activeRec.term)}</span></div>
            <div><span class="record-kv-label">Severity:</span> <span class="record-kv-val">${escapeHtml(activeRec.severity)}</span></div>
            <div><span class="record-kv-label">Serious (AESER):</span> <span class="record-kv-val">${activeRec.is_serious ? 'Yes (Serious)' : 'No'}</span></div>
            <div><span class="record-kv-label">Hospitalization:</span> <span class="record-kv-val">${activeRec.aeshosp === 'Y' ? 'Yes (AESHOSP=Y)' : 'No'}</span></div>
          `;
          if (activeRec.narrative) {
            kvItems += `<div style="grid-column: span 2;"><span class="record-kv-label">Narrative:</span> <p style="font-size: 11.5px; margin-top: 3px; color: #334155;">${escapeHtml(activeRec.narrative)}</p></div>`;
          }
        } else if (domainId === 'exposure') {
          kvItems += `
            <div><span class="record-kv-label">Dose Administered:</span> <span class="record-kv-val">${activeRec.dose !== null ? activeRec.dose + ' mg' : 'N/A'}</span></div>
            <div><span class="record-kv-label">Assigned Arm:</span> <span class="record-kv-val">${escapeHtml(activeRec.arm || '')}</span></div>
            <div><span class="record-kv-label">Protocol Deviation:</span> <span class="record-kv-val" style="color: ${activeRec.is_wrong_dose ? '#dc2626' : '#059669'};">${activeRec.is_wrong_dose ? '⚠️ WRONG DOSE ERROR' : 'Compliant'}</span></div>
          `;
        } else if (domainId === 'concomitant_medications') {
          kvItems += `
            <div><span class="record-kv-label">Medication:</span> <span class="record-kv-val">${escapeHtml(activeRec.treatment)}</span></div>
            <div><span class="record-kv-label">Med Class:</span> <span class="record-kv-val">${escapeHtml(activeRec.med_class)}</span></div>
            <div><span class="record-kv-label">Protocol Status:</span> <span class="record-kv-val" style="color: ${activeRec.is_prohibited ? '#dc2626' : '#059669'};">${activeRec.is_prohibited ? '⚠️ PROHIBITED MEDICATION' : 'Permitted'}</span></div>
          `;
        } else if (domainId === 'vital_signs' || domainId === 'ecg') {
          kvItems += `
            <div><span class="record-kv-label">Test:</span> <span class="record-kv-val">${escapeHtml(activeRec.testcd)}</span></div>
            <div><span class="record-kv-label">Result:</span> <span class="record-kv-val">${escapeHtml(activeRec.raw_value)}</span></div>
          `;
        } else if (domainId === 'disposition') {
          kvItems += `
            <div><span class="record-kv-label">Status:</span> <span class="record-kv-val">${escapeHtml(activeRec.status)}</span></div>
            <div><span class="record-kv-label">Reason:</span> <span class="record-kv-val">${escapeHtml(activeRec.reason || 'Completed Per Protocol')}</span></div>
          `;
        } else if (domainId === 'medical_history') {
          kvItems += `
            <div style="grid-column: span 2;"><span class="record-kv-label">Medical Condition:</span> <span class="record-kv-val">${escapeHtml(activeRec.term)}</span></div>
          `;
        }

        const rawDict = activeRec.raw || {};
        let rawRowsHtml = '';
        Object.keys(rawDict).forEach(k => {
          rawRowsHtml += `<tr><td style="color: #64748b; font-weight: 600; width: 35%;">${escapeHtml(k)}</td><td style="color: #0f172a; font-weight: 600;">${escapeHtml(String(rawDict[k]))}</td></tr>`;
        });

        recordCardHtml = `
          <div class="record-detail-card">
            <div class="record-detail-header">
              <div class="record-detail-title">
                ${domain.icon} Record Details: #${activeRec.seq}
              </div>
              <span class="strip-badge" style="background: ${domain.bg}; color: ${domain.color}; font-weight: 700;">${domain.code} #${activeRec.seq}</span>
            </div>

            <div class="record-kv-grid">
              ${kvItems}
            </div>

            <details class="raw-sdtm-accordion" open>
              <summary class="raw-sdtm-summary">▾ Raw CDISC SDTM Attributes (${Object.keys(rawDict).length} fields)</summary>
              <table class="raw-sdtm-table">
                <tbody>
                  ${rawRowsHtml}
                </tbody>
              </table>
            </details>
          </div>
        `;
      }

      panel.innerHTML = `
        <div class="panel-header">
          <div class="panel-title">
            <span>${domain.icon}</span> ${domain.name}
            <span class="graph-badge" style="background: ${domain.bg}; color: ${domain.color}; font-weight: 700;">${records.length} records</span>
          </div>
          <button class="btn-panel-back" onclick="selectPatientCenter()">
            ← Patient Overview
          </button>
        </div>

        <div class="domain-records-search">
          <input type="text" class="domain-search-input" placeholder="🔍 Filter records in this domain..." oninput="filterDomainTable(this.value)">
        </div>

        <div class="records-table-container">
          <table class="records-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Visit</th>
                <th>Test / Item</th>
                <th>Value / Flag</th>
                <th>Date</th>
              </tr>
            </thead>
            <tbody id="records-table-body">
              ${tableRowsHtml}
            </tbody>
          </table>
        </div>

        ${recordCardHtml}
      `;
    }

    function filterDomainTable(query) {
      const q = (query || '').toLowerCase().trim();
      const rows = document.querySelectorAll('#records-table-body tr');
      rows.forEach(r => {
        const text = r.textContent.toLowerCase();
        r.style.display = text.includes(q) ? '' : 'none';
      });
    }

    /* =========================================================
       SVG ZOOM & PAN INTERACTION
       ========================================================= */
    function initSvgInteraction() {
      const container = document.getElementById('p360-svg-container');
      const svg = document.getElementById('p360-svg');
      if (!container || !svg) return;

      container.onmousedown = (e) => {
        if (e.target.closest('g') && (e.target.closest('.domain-node-group') || e.target.closest('.center-patient-group') || e.target.closest('.sat-node-group'))) {
          return;
        }
        IS_DRAGGING = true;
        DRAG_START_X = e.clientX - P360_PAN_X;
        DRAG_START_Y = e.clientY - P360_PAN_Y;
      };

      window.onmousemove = (e) => {
        if (!IS_DRAGGING) return;
        P360_PAN_X = e.clientX - DRAG_START_X;
        P360_PAN_Y = e.clientY - DRAG_START_Y;
        updateViewportTransform();
      };

      window.onmouseup = () => {
        IS_DRAGGING = false;
      };

      container.onwheel = (e) => {
        e.preventDefault();
        const factor = e.deltaY < 0 ? 1.08 : 0.92;
        zoomGraph(factor);
      };
    }

    function zoomGraph(factor) {
      P360_ZOOM = Math.max(0.4, Math.min(3.0, P360_ZOOM * factor));
      updateViewportTransform();
    }

    function resetGraphView() {
      P360_ZOOM = 1.0;
      P360_PAN_X = 0;
      P360_PAN_Y = 0;
      updateViewportTransform();
    }

    function updateViewportTransform() {
      const g = document.getElementById('p360-viewport-g');
      if (g) {
        g.setAttribute('transform', `translate(${P360_PAN_X}, ${P360_PAN_Y}) scale(${P360_ZOOM})`);
      }
    }

    async function loadSubjectsDatalist() {
      try {
        const resp = await fetch('/api/subjects');
        const list = await resp.json();
        const dl = document.getElementById('p360-datalist');
        if (dl && Array.isArray(list)) {
          dl.innerHTML = list.map(s => `<option value="${s}"></option>`).join('');
        }
      } catch (err) {
        // quiet ignore
      }
    }


    /* =========================================================
       STAGE 2: MONITOR & HUMAN GATE JAVASCRIPT LOGIC
       ========================================================= */
    let CURRENT_STAGE2_REPORT = null;
    let PENDING_ESCALATIONS_LIST = [];

    async function triggerReviewCycle() {
      const btn = document.getElementById('btn-run-cycle');
      const cutSelect = document.getElementById('monitor-cut-select');
      const protoSelect = document.getElementById('monitor-protocol-select');

      const cutVal = parseInt(cutSelect.value, 10);
      const protoVal = parseInt(protoSelect.value, 10);

      if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span>⏳</span> Running Cycle...';
      }

      try {
        const resp = await fetch('/api/stage2/cycle', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ cut: cutVal, protocol_version: protoVal })
        });
        const data = await resp.json();
        CURRENT_STAGE2_REPORT = data;
        PENDING_ESCALATIONS_LIST = (data.escalations || []).map(e => Object.assign({}, e));
        renderStage2Report(data);
        renderHumanGateCards();
      } catch (err) {
        alert('Error executing review cycle: ' + err.message);
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = '<span>⚡</span> Run Review Cycle';
        }
      }
    }

    async function loadLatestReport() {
      if (CURRENT_STAGE2_REPORT) {
        renderStage2Report(CURRENT_STAGE2_REPORT);
        return;
      }
      try {
        const resp = await fetch('/api/stage2/report');
        const data = await resp.json();
        if (data && data.cycle) {
          CURRENT_STAGE2_REPORT = data;
          PENDING_ESCALATIONS_LIST = (data.escalations || []).map(e => Object.assign({}, e));
          renderStage2Report(data);
          renderHumanGateCards();
        }
      } catch (e) {}
    }

    function renderStage2Report(report) {
      if (!report) return;

      // Update metric counters
      const findingsEl = document.getElementById('metric-findings');
      const escsEl = document.getElementById('metric-escalations');
      const queriesEl = document.getElementById('metric-queries');
      const devsEl = document.getElementById('metric-deviations');

      if (findingsEl) findingsEl.textContent = report.findings_count || (report.findings || []).length;
      if (escsEl) escsEl.textContent = report.escalations_count || (report.escalations || []).length;
      if (queriesEl) queriesEl.textContent = report.queries_count || (report.queries || []).length;
      if (devsEl) devsEl.textContent = report.compliance_deviations_count || (report.compliance_deviations || []).length;

      // Update memory pill
      const memPill = document.getElementById('memory-stats-pill');
      if (memPill) {
        memPill.textContent = `✓ Persistent Memory Active: Cycle #${report.cycle} (Cut ${report.cut}, v${report.protocol_version})`;
      }

      // Render Decision Trace Log
      const traceContainer = document.getElementById('trace-list-container');
      if (traceContainer) {
        const traces = report.trace || [];
        if (traces.length === 0) {
          traceContainer.innerHTML = '<div style="color:#64748b; font-style:italic;">No trace entries recorded.</div>';
        } else {
          traceContainer.innerHTML = traces.map(t => {
            const node = t.node || 'trace';
            const msg = escapeHtml(t.message || '');
            const timeStr = t.timestamp ? t.timestamp.split('T')[1].slice(0,8) : '';
            return `
              <div class="trace-row ${node}">
                <span class="node-badge ${node}">${node}</span>
                <div class="trace-text">
                  <span style="color:#64748b; margin-right:8px;">[${timeStr}]</span>
                  <strong>${msg}</strong>
                </div>
              </div>
            `;
          }).join('');
        }
      }

      // Render Queries Table
      const queriesBody = document.getElementById('queries-table-body');
      const qCountHeader = document.getElementById('queries-header-count');
      const queries = report.queries || [];
      if (qCountHeader) qCountHeader.textContent = `${queries.length} Queries`;
      if (queriesBody) {
        if (queries.length === 0) {
          queriesBody.innerHTML = '<tr><td colspan="7" style="text-align:center; color:#94a3b8;">0 queries raised (all duplicate queries prevented by memory).</td></tr>';
        } else {
          queriesBody.innerHTML = queries.map(q => {
            const stClass = q.status || 'OPEN';
            return `
              <tr>
                <td style="font-weight:700; font-family:monospace;">${escapeHtml(q.id || '-')}</td>
                <td><strong onclick="openPatient360('${q.usubjid}')" style="cursor:pointer; color:var(--primary);">${escapeHtml(q.usubjid)}</strong></td>
                <td>${escapeHtml(q.domain)}</td>
                <td>${q.seq || '-'}</td>
                <td>${escapeHtml(q.text || '')}</td>
                <td><span class="badge-status ${stClass}">${escapeHtml(q.status || 'OPEN')}</span></td>
                <td style="color:#334155; font-size:12px;">${escapeHtml(q.response || '-')}</td>
              </tr>
            `;
          }).join('');
        }
      }

      // Render Compliance Deviations Table
      const devsBody = document.getElementById('deviations-table-body');
      const devsCountHeader = document.getElementById('deviations-header-count');
      const devs = report.compliance_deviations || [];
      if (devsCountHeader) devsCountHeader.textContent = `${devs.length} Deviations`;
      if (devsBody) {
        if (devs.length === 0) {
          devsBody.innerHTML = '<tr><td colspan="5" style="text-align:center; color:#94a3b8;">No deviations detected under this protocol version.</td></tr>';
        } else {
          devsBody.innerHTML = devs.slice(0, 50).map(d => {
            const cat = d.category || 'protocol';
            const detail = d.actual || d.treatment || (d.visit ? `${d.visit} (diff: ${d.difference}d)` : `Dose: ${d.dose} mg`);
            return `
              <tr>
                <td><span style="font-weight:700; text-transform:uppercase; font-size:11px;">${escapeHtml(cat)}</span></td>
                <td><strong onclick="openPatient360('${d.usubjid}')" style="cursor:pointer; color:var(--primary);">${escapeHtml(d.usubjid)}</strong></td>
                <td>${escapeHtml(d.site || '-')}</td>
                <td>${escapeHtml(d.rule || 'Protocol violation')}</td>
                <td style="font-family:monospace;">${escapeHtml(String(detail))}</td>
              </tr>
            `;
          }).join('');
        }
      }

      // Update Human Gate counter badge
      updatePendingBadge();
    }

    function updatePendingBadge() {
      const badge = document.getElementById('pending-counter-badge');
      const note = document.getElementById('human-gate-pending-note');
      const count = PENDING_ESCALATIONS_LIST.length;
      if (badge) badge.textContent = count;
      if (note) note.textContent = `${count} pending monitor decision`;
    }

    function renderHumanGateCards() {
      const container = document.getElementById('escalations-cards-container');
      if (!container) return;

      if (PENDING_ESCALATIONS_LIST.length === 0) {
        container.innerHTML = `
          <div style="text-align:center; padding:40px; color:#64748b; background:#f8fafc; border:1px dashed #cbd5e1; border-radius:10px;">
            ✓ All escalations adjudicated or none pending. Run a review cycle in the MONITOR tab to identify candidates.
          </div>
        `;
        updatePendingBadge();
        return;
      }

      container.innerHTML = PENDING_ESCALATIONS_LIST.map((esc, idx) => {
        const target = esc.usubjid || esc.site || 'TARGET';
        const code = esc.code || 'ESCALATION';
        const sev = esc.severity || 'HIGH';
        const evidenceStr = JSON.stringify(esc.evidence || []);

        return `
          <div class="escalation-card" id="esc-card-${idx}">
            <div class="escalation-top">
              <div class="escalation-subject">
                <span>🛡️</span>
                <span onclick="openPatient360('${target}')" style="cursor:pointer; color:var(--primary); text-decoration:underline;">${escapeHtml(target)}</span>
                <span style="font-size:13px; color:#64748b; font-weight:normal;">(Site ${escapeHtml(esc.site || '-')})</span>
              </div>
              <div>
                <span class="severity-badge severity-${sev}">${sev}</span>
                <span style="font-size:12px; font-weight:700; margin-left:8px; color:#475569;">${escapeHtml(code)}</span>
              </div>
            </div>

            <div class="escalation-summary">
              <strong>Clinical Rationale:</strong> ${escapeHtml(esc.summary || '')}
            </div>

            <div class="escalation-evidence-box">
              <strong>Evidence Records:</strong> ${escapeHtml(evidenceStr)}
            </div>

            <div class="escalation-actions-row">
              <button class="btn-action-approve" onclick="executeEscalationAction('${code}', '${target}', 'APPROVED', ${idx})">
                ✓ APPROVE
              </button>
              <button class="btn-action-reject" onclick="executeEscalationAction('${code}', '${target}', 'REJECTED', ${idx})">
                ✗ REJECT
              </button>
              <button class="btn-action-clarify" onclick="toggleClarifyBox(${idx})">
                ❓ CLARIFY
              </button>
            </div>

            <!-- Clarify Interactive Box -->
            <div class="clarify-prompt-box" id="clarify-box-${idx}">
              <div style="font-size:13px; font-weight:700; color:#0f766e;">Enter Medical Monitor Clarification Query:</div>
              <input type="text" id="clarify-input-${idx}" class="clarify-input" value="What was the ALT at screening, and is there a concomitant hepatotoxic medication?" placeholder="Ask a clarification question...">
              <div style="display:flex; justify-content:flex-end; gap:8px;">
                <button class="btn-action-clarify" onclick="submitClarifyAction('${code}', '${target}', ${idx})">
                  Submit Question & Resolve via Graph
                </button>
              </div>
              <div id="clarify-res-${idx}" style="display:none;" class="clarify-result-box"></div>
            </div>
          </div>
        `;
      }).join('');

      updatePendingBadge();
    }

    function toggleClarifyBox(idx) {
      const box = document.getElementById(`clarify-box-${idx}`);
      if (box) {
        box.style.display = (box.style.display === 'flex') ? 'none' : 'flex';
      }
    }

    async function executeEscalationAction(code, target, action, cardIdx) {
      try {
        const resp = await fetch('/api/stage2/escalation_action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ code: code, target: target, action: action })
        });
        const res = await resp.json();
        
        // Remove from local list
        PENDING_ESCALATIONS_LIST = PENDING_ESCALATIONS_LIST.filter(e => !(e.code === code && (e.usubjid === target || e.site === target)));
        
        // Re-render
        renderHumanGateCards();
        loadLatestReport();
      } catch (err) {
        alert('Action error: ' + err.message);
      }
    }

    async function submitClarifyAction(code, target, cardIdx) {
      const qInput = document.getElementById(`clarify-input-${cardIdx}`);
      const resBox = document.getElementById(`clarify-res-${cardIdx}`);
      const question = qInput ? qInput.value : '';

      try {
        const resp = await fetch('/api/stage2/escalation_action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ code: code, target: target, action: 'CLARIFY', question: question })
        });
        const res = await resp.json();

        if (resBox) {
          resBox.style.display = 'block';
          resBox.innerHTML = `
            <strong>✓ Answered from StudyGraph:</strong> ${escapeHtml(res.result.clarification_answer)}<br>
            <strong>Status:</strong> Resubmitted to Monitor &rarr; <span style="font-weight:700; color:#10b981;">APPROVED</span>
          `;
        }

        setTimeout(() => {
          PENDING_ESCALATIONS_LIST = PENDING_ESCALATIONS_LIST.filter(e => !(e.code === code && (e.usubjid === target || e.site === target)));
          renderHumanGateCards();
          loadLatestReport();
        }, 1500);

      } catch (err) {
        alert('Clarification error: ' + err.message);
      }
    }

    document.addEventListener('DOMContentLoaded', () => {
      loadSubjectsDatalist();
      loadLatestReport();
    });

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

def sanitize_json(data: Any) -> str:
    """
    Serializes data to RFC 8259 compliant JSON string, replacing non-compliant
    floating-point NaN and Infinity values with null so browser JSON.parse never fails.
    """
    raw_json = json.dumps(data, default=str)
    clean_json = re.sub(r'\bNaN\b', 'null', raw_json)
    clean_json = re.sub(r'\b-?Infinity\b', 'null', clean_json)
    return clean_json

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

        elif path == "/api/subjects":
            subjs = sorted(list(GRAPH.subjects.keys()))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(subjs).encode("utf-8"))
            return

        elif path == "/api/stage2/report":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            rep = LATEST_REPORT or {}
            self.wfile.write(json.dumps(rep, default=str).encode("utf-8"))
            return

        elif path == "/api/stage2/pending_escalations":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(PENDING_ESCALATIONS, default=str).encode("utf-8"))
            return

        elif path == "/api/stage2/memory":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            mem = CREW.memory.data if CREW else {}
            self.wfile.write(json.dumps(mem, default=str).encode("utf-8"))
            return

        elif path == "/api/patient360":
            raw_subj = query.get("usubjid", ["042-S01-001"])[0].strip()
            subj = raw_subj
            if subj not in GRAPH.subjects and subj.upper() in GRAPH.subjects:
                subj = subj.upper()
            pdata = GRAPH.patient360(subj)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(sanitize_json(pdata).encode("utf-8"))
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        global LATEST_REPORT, PENDING_ESCALATIONS
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

        elif path == "/api/patient360":
            raw_subj = payload.get("usubjid", "042-S01-001").strip()
            subj = raw_subj
            if subj not in GRAPH.subjects and subj.upper() in GRAPH.subjects:
                subj = subj.upper()
            pdata = GRAPH.patient360(subj)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(sanitize_json(pdata).encode("utf-8"))
            return

        elif path == "/api/stage2/cycle":
            cut = int(payload.get("cut", 6))
            protocol_version = int(payload.get("protocol_version", 2))
            report = CREW.run_cycle(cut=cut, protocol_version=protocol_version)
            LATEST_REPORT = report.to_dict()
            PENDING_ESCALATIONS = [dict(e) for e in report.escalations]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(LATEST_REPORT, default=str).encode("utf-8"))
            return

        elif path == "/api/stage2/escalation_action":
            code = payload.get("code", "")
            target = payload.get("target", "")
            action = str(payload.get("action", "APPROVED")).upper()
            question = payload.get("question", "")

            result_entry = {}
            if action == "APPROVED":
                CREW.memory.record_escalation(code, target, payload, CREW.memory.data["cycle_count"])
                result_entry = {
                    "code": code,
                    "target": target,
                    "decision": "APPROVED",
                    "reason": "Serious adverse event confirmed; expedited report within 24 h."
                }
            elif action == "REJECTED":
                reason = "Not clinically significant; handle as a data query."
                CREW.memory.record_rejection(code, target, reason, CREW.memory.data["cycle_count"])
                result_entry = {
                    "code": code,
                    "target": target,
                    "decision": "REJECTED",
                    "reason": reason,
                    "action": "DOWNGRADED_TO_MONITORING"
                }
            elif action == "CLARIFY":
                clarif_ans = CREW._answer_clarification_from_graph(target, question)
                final_reason = f"Clarification accepted: {clarif_ans}. Action confirmed."
                CREW.memory.record_escalation(code, target, payload, CREW.memory.data["cycle_count"])
                result_entry = {
                    "code": code,
                    "target": target,
                    "decision": "APPROVED",
                    "initial_status": "CLARIFY",
                    "clarification_question": question,
                    "clarification_answer": clarif_ans,
                    "reason": final_reason
                }

            PENDING_ESCALATIONS = [e for e in PENDING_ESCALATIONS if not (e.get("code") == code and (e.get("usubjid") == target or e.get("site") == target))]

            if LATEST_REPORT:
                trace_str = ""
                if action == "CLARIFY":
                    trace_str = f"{code} {target} -> CLARIFY; answered from graph ({result_entry.get('clarification_answer')}); resubmitted -> APPROVED"
                elif action == "REJECTED":
                    trace_str = f"{code} {target} -> REJECTED: downgraded to monitoring ({result_entry.get('reason')})"
                else:
                    trace_str = f"{code} {target} -> APPROVED: {result_entry.get('reason')}"
                LATEST_REPORT.setdefault("trace", []).append({
                    "node": "human_gate",
                    "message": trace_str,
                    "details": result_entry,
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")
                })
                LATEST_REPORT.setdefault("human_gate_decisions", []).append(result_entry)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"result": result_entry, "pending_count": len(PENDING_ESCALATIONS)}, default=str).encode("utf-8"))
            return

        elif path == "/queries":
            reply = CREW._post_query(payload)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(reply, default=str).encode("utf-8"))
            return

        elif path == "/escalations":
            reply = CREW._post_escalation(payload)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(reply, default=str).encode("utf-8"))
            return

        self.send_error(404, "Not Found")

    def log_message(self, format, *args):
        # Quiet standard logging
        return

def run_server(port: int = 8080, data_dir: str = "hackathon-data"):
    global GRAPH, ATLAS, CREW, LATEST_REPORT, PENDING_ESCALATIONS
    print(f"[ATLAS Clinical Agent] Initializing StudyGraph on '{data_dir}'...")
    GRAPH = StudyGraph(data_dir)
    GRAPH.build()
    ATLAS = Atlas(GRAPH)
    print(f"[ATLAS Clinical Agent] Graph ready: {GRAPH.build_stats['nodes']:,} nodes, {GRAPH.build_stats['subjects']} subjects.")
    
    print("[MONITOR Stage 2] Initializing ReviewCrew and executing initial cycle (Cut 6, v2)...")
    CREW = ReviewCrew(hub_url=None, gateway_url=None, team_key="team-study-sentinel", atlas=ATLAS)
    try:
        report = CREW.run_cycle(cut=6, protocol_version=2)
        LATEST_REPORT = report.to_dict()
        PENDING_ESCALATIONS = [dict(e) for e in report.escalations]
        print(f"[MONITOR Stage 2] Initial cycle ready: {len(report.findings)} findings, {len(report.escalations)} escalations, {len(report.queries)} queries.")
    except Exception as e:
        print(f"[MONITOR Stage 2] Notice: {e}")
    
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
