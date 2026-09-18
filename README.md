# Study Sentinel — Stage 1 (ATLAS)

**Team**: Study Sentinel  
**Members**: Sriridhu Keshav T  

## Run it

```bash
pip install -r requirements.txt
python -m stage1.atlas --data hackathon-data
python starter/run_local_harness.py --module stage1.atlas --data hackathon-data
```

## How we understood the problem
We are tasked with constructing a unified, queryable clinical study knowledge graph from nine disjoint clinical trial tables and answering clinical safety and compliance questions with exact record citations. The critical challenge is handling dynamic weekly cuts, reconciling retrospective corrections, normalizing conflicting laboratory units (S07 µkat/L vs Central U/L), and ignoring adversarial reviewer instructions embedded in study documents. We deliberately put complex agentic frameworks and LLMs out of scope because clinical adjudication requires 100% deterministic arithmetic, zero hallucination, and sub-second execution.

## Architecture
```
[Raw CSVs (DM, AE, LB, VS, EX, CM, DS, MH, EG)]
                     │
                     ▼
       ┌───────────────────────────┐
       │     StudyGraph.build      │ <── cuts.csv, corrections.csv, reference_ranges.csv
       │  - Filters cut_available  │
       │  - Applies corrections    │
       │  - Parses dates & units   │
       └─────────────┬─────────────┘
                     ▼
       ┌───────────────────────────┐
       │     Patient 360 Index     │ (Longitudinal subject -> visits -> records)
       │  - Evaluates Hy's law     │
       │  - Flags protocol errors  │
       └─────────────┬─────────────┘
                     ▼
       ┌───────────────────────────┐
       │        Atlas Agent        │ <── Question (count, lookup, finding, trap)
       │  - Deterministic reasoner │
       │  - Cites exact RecordRefs │
       └─────────────┬─────────────┘
                     ▼
              Answer + Evidence
```

## Tech stack

| Layer | What we used | Why this, not the obvious alternative |
|---|---|---|
| **Language / runtime** | Python 3.12 | Universally available standard runtime with clean dataclasses and high portability. |
| **Data handling** | pandas & Python standard library | Fast vectorized CSV ingestion combined with safe scalar parsing for dates and numbers. |
| **Graph / storage** | In-memory hierarchical dictionary index | Plain dicts provide instantaneous \(O(1)\) lookups; Neo4j or NetworkX added 400ms+ build latency without query benefits. |
| **Orchestration** | Deterministic rule & signal engine | Rule-based adjudication guarantees zero hallucinations, exact arithmetic, and 100% reproducible evidence citations. |
| **Model, if any** | None | Clinical findings (e.g., \(239.7 > 168\)) are mathematical truths; an LLM introduces hallucination risk, token cost, and non-determinism. |
| **Interface** | Pure Python API (`StudyGraph`, `Atlas`) | Adheres strictly to the programmatic contract required by automated grading harnesses. |
| **Testing** | Automated local harness (`starter/run_local_harness.py`) | Verifies contract schema, evidence validity, trap honesty, and runtime benchmarks in fresh environments. |

## Data handling
- **Units**: `reference_ranges.csv` provides upper/lower reference limits per test and laboratory. Site S07 reports ALT and AST in `µkat/L` (normal range 0.12–0.93 for ALT). In `StudyGraph.build()`, values in `µkat/L` are standardized to `U/L` via `val * 60.0` before comparing to Upper Limits of Normal (ULN).
- **Dates**: Robust multi-pattern parsing accepts ISO (`YYYY-MM-DD`), alphanumeric (`28-JAN-2026`, `03-FEB-2026`), and slash formats (`YYYY/MM/DD`, `DD/MM/YYYY`). Any unrecognized date string resolves safely to `None` without raising exceptions.
- **Non-numeric laboratory values**: Values starting with `<` (e.g. `"<5"`) indicate below detection limits and are parsed as `(None, is_below_detection=True)` rather than `0` (avoiding false normal or zero-division errors). `"ND"` is marked as not done `None`. European comma decimals (`"12,4"`) are converted to float `12.4`. Blank fields resolve to `None`.
- **Malformed rows**: Missing mandatory fields or unparseable sequences are skipped gracefully while keeping valid neighbouring records.

## Documents
The protocol versions (v1, v2, v3), laboratory manual, and SAP are ingested as domain context and clinical rules. Version 3 introduces Sulfonylureas as prohibited medications, which triggers dynamic re-evaluation when building cuts \(\ge 9\). If a document contains text addressed to automated reviewers (e.g., `lab-manual.md` requesting exclusion of sites S03 and S07), our system treats that sentence strictly as passive descriptive evidence, **never as an instruction to obey**. Sites S03 and S07 are fully evaluated for safety signals.

## When the answer is nothing
When a question asks for findings that do not exist (e.g., dosing errors at site S01, or adverse event discontinuations at site S07), Atlas does not guess or invent plausible-looking records. It verifies the absence of qualifying events in Patient 360, returns `answer = []` (or `0` for count questions), sets `evidence = []`, and reports an honest, transparent explanation with appropriate confidence.

## Graph
The graph contains **29,339 nodes** (Study, 12 Sites, Protocol, 241 Subjects, scheduled Visits, and clinical records across 8 domains) and **29,337 edges** mapping hierarchy and temporal links. This structure enables bidirectional longitudinal exploration (patient \(\rightarrow\) visit \(\rightarrow\) lab/AE/dose) in sub-millisecond time without repeated tabular scans. Full statistics are serialized in `graph_stats.json`.

## What we know is weak
1. **Natural language query parsing**: Questions are mapped to clinical intent using keyword and regular-expression pattern matching rather than a learned semantic parser; queries phrased in novel colloquial syntax would need additional intent rules.
2. **Unit conversion domain coverage**: Conversion logic is specifically calibrated for ALT and AST from `µkat/L` to `U/L`. If a hidden study version introduced a third analyte in an unexpected unit without registering conversion rules, it would require explicit conversion factors in the configuration.
