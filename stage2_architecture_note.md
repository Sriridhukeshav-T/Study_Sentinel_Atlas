# Study Sentinel — Stage 2 (MONITOR) Architecture Note

**System Overview:**
Stage 2 (MONITOR) builds an autonomous multi-agent clinical trial review crew that extends Stage 1 (ATLAS) without modifying public Stage 1 interfaces. It orchestrates six sequential nodes, enforces cross-cycle persistent memory, supports real-time decision tracing, and integrates a responsive human gate for medical monitor adjudication.

---

## 1. Six Node Responsibilities & Orchestration Order

The ReviewCrew executes six nodes in strict deterministic order per review cycle:

1. **`detect` (Stage 1 Atlas Integration):**
   - Reuses the existing `StudyGraph` and `Atlas` agent without modification.
   - Dynamically indexes the clinical cut and active protocol version.
   - Detects safety signals (potential Hy's law candidates, serious adverse events, miscoded SAEs), data discrepancies (AEs prior to first dose, wrong doses, duplicate subjects, unit mismatches), and compliance deviations.
   - Immediately logs a decision trace entry detailing detection totals and category counts.

2. **`medical_review` (Physician Review & Clinical Judgement):**
   - Evaluates severity, plausibility, and escalation urgency.
   - **Critical SAE Adjudication:** Enforces protocol §6 where any hospitalization (`AESHOSP = 'Y'`) makes an adverse event serious regardless of site coding (`AESER = 'N'`), generating critical expedited escalation drafts.
   - **Baseline Liver Candidate Handling:** Inspects screening transaminases (`ALT`/`AST`) for Hy's law candidates; candidates with pre-existing baseline elevation are kept as `MONITORING_ONLY` rather than escalated, recording explicit rationale.
   - **Systemic Site Patterns:** Aggregates recurring errors (e.g., site S09 dosing deviations) into site-level escalation drafts.
   - **Multi-Cycle Recurrence:** Automatically escalates subjects flagged across multiple cycles.

3. **`data_manager` (Data Quality & Queries):**
   - Identifies actionable data quality discrepancies (e.g., AE start date preceding first dose date, duplicate subject enrollment across multiple sites, dose transcription errors).
   - Generates specific, record-cited queries citing subject, domain, sequence number, and actionable instructions.
   - Submits queries via `POST /queries`. Tracks site responses (`CLOSED` vs `OPEN` / on-hold).
   - Enforces strict deduplication against persistent memory to guarantee zero duplicate queries.

4. **`compliance` (Protocol In-Force Verification):**
   - Dynamically evaluates every subject against the protocol version currently in force at the requested data cut.
   - Assesses screening eligibility (age 18–75), prohibited concomitant medications (Systemic Glucocorticoids in v1/v2; Sulfonylureas added in v3), visit windows (±7 days in v1; tightened to ±3 days in v2/v3), and renal exclusions (Creatinine > 1.5 mg/dL at screening added in v2).

5. **`human_gate` (Medical Monitor Adjudication):**
   - Dispatches clinical escalations to the medical monitor desk via `POST /escalations`.
   - Adjudicates and executes monitor responses for all three possible pathways (`APPROVED`, `REJECTED`, `CLARIFY`).

6. **`execute` (Cycle Finalization & Reporting):**
   - Compiles the final `ReviewReport` containing findings, escalations, queries, compliance deviations, human-gate decisions, and real-time trace entries.
   - Flushes persistent memory state to disk and logs the cycle completion trace.

---

## 2. Memory Design Across Cycles

Persistent cross-cycle state is maintained via `PersistentMemory` (`stage2_memory.json`):
- **Query Deduplication:** Tracks all raised queries by `DOMAIN|USUBJID|SEQ`. If a record has already been queried in any prior cycle, it is strictly bypassed.
- **Escalation Deduplication:** Tracks escalations by `CODE|TARGET`. Prevents re-raising active or closed escalations.
- **Rejection Memory:** Stores monitor-rejected issues in `rejected_escalations`. Guaranteed never to re-escalate in subsequent cycles.
- **Subject Cycle Flagging:** Records flagged data cuts per subject. A subject flagged across multiple distinct data cuts is automatically escalated under `RECURRING_SUBJECT_FLAG`.
- **Site Problem Accumulation:** Tracks cumulative errors per site, generating site-level flags for systematic GCP issues.
- **Idempotency Guarantee:** Re-running the exact same data cut produces **0 new queries** and **0 duplicate escalations**.

---

## 3. Human Gate Decision Handling

The crew handles all three medical monitor responses deterministically:

- **`APPROVED`:**
  - Executes clinical workflow (e.g., triggers 24-hour expedited safety reporting, holds study drug, or issues site corrective action).
  - Records the decision in memory and writes to trace:  
    `human_gate {CODE} {TARGET} -> APPROVED: {reason}`

- **`REJECTED`:**
  - Downgrades the escalation to monitoring with recorded monitor rationale.
  - Registers the rejection key in persistent memory.
  - Guaranteed **not dropped** from tracking and **never re-escalated** in subsequent cycles.
  - Trace:  
    `human_gate {CODE} {TARGET} -> REJECTED: downgraded to monitoring ({reason})`

- **`CLARIFY`:**
  - Recognized strictly as an **in-flight clarification request**, never as a rejection.
  - The crew parses the monitor's clinical inquiry, inspects the in-memory `StudyGraph` / Patient 360 data, and autonomously answers the query (e.g., retrieves screening ALT values and checks concomitant medications in CM).
  - Re-submits the escalation draft with the graph-derived evidence.
  - Upon resubmission, the monitor approves the action.
  - Trace:  
    `human_gate {CODE} {TARGET} -> CLARIFY; answered from graph ({answer}); resubmitted -> APPROVED`

---

## 4. Mid-Stage Protocol Amendment / Data Cut Evolution

As clinical trials progress, study protocol rules amend partway through (e.g., Cut 4 under Protocol v1 transitioning to Cut 5/6 under Protocol v2, and Cut 9+ under Protocol v3):
- **What Changed in Code:** The `compliance` node checks protocol rules dynamically per cut rather than permanently caching historical compliance states.
- **Version Differentiation:**
  - *Protocol v1 (Cuts 1–4):* Visit window is ±7 days; 0 renal exclusions; only systemic glucocorticoids prohibited.
  - *Protocol v2 (Cuts 5–8):* Amendment 2 introduces renal impairment exclusion (`Creatinine > 1.5 mg/dL` at screening) and narrows visit windows to ±3 days. Subjects previously compliant now trigger deviations (e.g., subjects `042-S01-003`, `042-S06-003`, `042-S06-008`, `042-S11-017`).
  - *Protocol v3 (Cuts 9–12):* Amendment 3 adds Sulfonylurea class medications as prohibited concomitant medications.
- Dynamically recalculating compliance per requested cut guarantees full audit compliance across mid-stage study shifts without stale cache pollution.

---

## 5. Real-Time Trace Generation

- **Real-Time Writing:** Decisions are appended to `TraceLogger` **as they happen** inside each node, not retroactively generated from a post-hoc summary.
- **Trace Accountability:** Each trace record contains timestamp, node name (`detect`, `medical_review`, `data_manager`, `compliance`, `human_gate`, `execute`), action type, cited evidence, and human-readable decision text.
- Any node execution leaving no trace entry is treated as unexecuted.
