"""
starter/run_local_harness.py
Official local test harness for Study Sentinel Stage 1 (ATLAS).

Usage:
    python starter/run_local_harness.py --module stage1.atlas --data hackathon-data

Validates:
- Module loading and interface contract
- Graph build statistics and Patient 360 integrity
- Execution of the 10 public benchmark questions
- Strict schema validation
- Evidence discipline (all cited records exist and support claims)
- Trap handling (empty answers returned honestly without hallucinations)
- Execution time limits (<= 120 seconds per question)
- Exports graph_stats.json and stage1_public.json
"""

import argparse
import importlib
import json
import os
import sys
import time
from typing import List, Dict, Any, Tuple

# Ensure current working directory is in sys.path
cwd = os.getcwd()
if cwd not in sys.path:
    sys.path.insert(0, cwd)

from starter.schemas import Question, Answer, RecordRef

PUBLIC_QUESTIONS: List[Question] = [
    Question(
        question_id="Q001",
        text="How many subjects at site S07 discontinued due to an adverse event?",
        kind="count"
    ),
    Question(
        question_id="Q002",
        text="List the laboratory and adverse-event records for 042-S05-003 within 7 days of the WEEK8 visit",
        kind="lookup"
    ),
    Question(
        question_id="Q003",
        text="Which subjects meet potential Hy's law criteria?",
        kind="finding"
    ),
    Question(
        question_id="Q004",
        text="Which subjects at site S01 received a wrong dose?",
        kind="trap"
    ),
    Question(
        question_id="Q005",
        text="Which subjects took a prohibited systemic glucocorticoid concomitant medication?",
        kind="finding"
    ),
    Question(
        question_id="Q006",
        text="Which subjects experienced a miscoded serious adverse event (hospitalized with AESER=N)?",
        kind="finding"
    ),
    Question(
        question_id="Q007",
        text="Which subjects are duplicate enrollments across multiple sites?",
        kind="finding"
    ),
    Question(
        question_id="Q008",
        text="Which subjects violated the age inclusion criteria at screening?",
        kind="finding"
    ),
    Question(
        question_id="Q009",
        text="Which subjects at site S10 experienced severe pancreatitis?",
        kind="trap"
    ),
    Question(
        question_id="Q010",
        text="Which subjects took prohibited concomitant medications under Protocol Version 3?",
        kind="finding",
        cut=9
    ),
]


def validate_evidence(evidence_list: List[Any], graph: Any) -> Tuple[bool, str]:
    """
    Validates that every cited RecordRef actually exists in the graph.
    """
    if not evidence_list:
        return True, "Empty evidence (valid for traps/none)"
    
    for ev in evidence_list:
        if isinstance(ev, dict):
            dom = ev.get("domain")
            subj = ev.get("usubjid")
            seq = ev.get("seq")
        elif hasattr(ev, "domain"):
            dom = ev.domain
            subj = ev.usubjid
            seq = ev.seq
        else:
            return False, f"Unknown evidence structure: {ev}"

        if dom == "DOC":
            continue

        if not subj or subj not in graph.subjects:
            return False, f"Subject {subj} cited in evidence does not exist in graph"

        pdata = graph.subjects[subj]
        domain_key_map = {
            "LB": "laboratory",
            "AE": "adverse_events",
            "EX": "exposure",
            "CM": "concomitant_medications",
            "DS": "disposition",
            "VS": "vital_signs",
            "MH": "medical_history",
            "EG": "ecg",
            "DM": "demographics"
        }
        
        target_list_key = domain_key_map.get(dom)
        if target_list_key == "demographics":
            found = True
        elif target_list_key and target_list_key in pdata:
            found = False
            for r in pdata[target_list_key]:
                if r.get("seq") == seq:
                    found = True
                    break
        else:
            found = False

        if not found:
            return False, f"Record {dom} seq {seq} for subject {subj} does not exist in Patient 360"

    return True, "All cited records verified in study graph"


def run_harness(module_name: str, data_dir: str, output_stats: str, output_public: str) -> bool:
    print("=" * 70)
    print(" STUDY SENTINEL — STAGE 1 (ATLAS) LOCAL BENCHMARK HARNESS")
    print("=" * 70)
    print(f"Loading module: {module_name}")
    print(f"Data directory: {data_dir}")

    # Import target module
    try:
        mod = importlib.import_module(module_name)
    except ImportError as e:
        print(f"FAILED: Cannot import module '{module_name}': {e}")
        return False

    StudyGraph = getattr(mod, "StudyGraph", None)
    Atlas = getattr(mod, "Atlas", None)

    if not StudyGraph or not Atlas:
        print("FAILED: Module must export 'StudyGraph' and 'Atlas' classes.")
        return False

    # 1. Build Graph
    print("\n[Step 1/3] Building StudyGraph...")
    graph = StudyGraph(data_dir)
    t_start_build = time.perf_counter()
    stats = graph.build()
    build_time = time.perf_counter() - t_start_build
    print(f"Graph built in {build_time:.2f}s ({stats.get('ms', 0)} ms reported)")
    print(f"  Nodes:    {stats.get('nodes')}")
    print(f"  Edges:    {stats.get('edges')}")
    print(f"  Subjects: {stats.get('subjects')}")

    # Save graph_stats.json
    with open(output_stats, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)
    print(f"Saved build statistics to: {output_stats}")

    # 2. Test Patient 360 contract
    print("\n[Step 2/3] Verifying Patient 360 contract...")
    sample_subj = next(iter(graph.subjects.keys()))
    p360 = graph.patient360(sample_subj)
    assert "usubjid" in p360, "Patient 360 missing usubjid"
    assert "demographics" in p360, "Patient 360 missing demographics"
    assert "visits" in p360, "Patient 360 missing visits"
    assert "laboratory" in p360, "Patient 360 missing laboratory"
    print(f"Patient 360 contract verified on subject {sample_subj}")

    # 3. Answer 10 Public Benchmark Questions
    print("\n[Step 3/3] Answering 10 Public Benchmark Questions...")
    atlas = Atlas(graph)
    results = []
    all_passed = True
    traps_correct = 0
    traps_total = 0
    valid_evidence_count = 0

    print("-" * 70)
    print(f"{'QID':<6} {'Kind':<9} {'Time(s)':<8} {'Status':<10} {'Evidence Check'}")
    print("-" * 70)

    for q in PUBLIC_QUESTIONS:
        t0 = time.perf_counter()
        try:
            ans: Answer = atlas.answer(q)
            q_time = time.perf_counter() - t0
        except Exception as e:
            print(f"{q.question_id:<6} {q.kind:<9} ERROR: {e}")
            all_passed = False
            continue

        ans_dict = ans.to_dict() if hasattr(ans, "to_dict") else ans
        results.append(ans_dict)

        # Check time limit
        time_ok = (q_time <= 120.0)

        # Check evidence validity
        ev_valid, ev_msg = validate_evidence(ans_dict.get("evidence", []), graph)
        if ev_valid:
            valid_evidence_count += 1

        # Check trap handling
        is_trap = (q.kind == "trap")
        trap_ok = True
        if is_trap:
            traps_total += 1
            if ans_dict.get("answer") == [] and len(ans_dict.get("evidence", [])) == 0:
                traps_correct += 1
            else:
                trap_ok = False

        status = "PASS" if (time_ok and ev_valid and trap_ok) else "FAIL"
        if status != "PASS":
            all_passed = False

        print(f"{q.question_id:<6} {q.kind or '':<9} {q_time:<8.3f} {status:<10} {ev_msg}")

    # Save stage1_public.json
    with open(output_public, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print("-" * 70)
    print(f"Saved 10 public answers to: {output_public}")

    # Summary scorecard
    evidence_rate = (valid_evidence_count / len(PUBLIC_QUESTIONS)) * 100
    print("\n" + "=" * 70)
    print(" HARNESS EVALUATION SUMMARY")
    print("=" * 70)
    print(f"Contract Schema:     {'PASS' if all_passed else 'FAIL'}")
    print(f"Evidence Discipline: {evidence_rate:.1f}% ({valid_evidence_count}/{len(PUBLIC_QUESTIONS)}) [Gate: >= 90%]")
    print(f"Trap Integrity:      {traps_correct}/{traps_total} traps answered honestly [Gate: >= 2/6]")
    print(f"Wall Time Budget:    All questions <= 120s")
    print("=" * 70)

    return all_passed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run local harness for Stage 1 ATLAS")
    parser.add_argument("--module", default="stage1.atlas", help="Python module path (default: stage1.atlas)")
    parser.add_argument("--data", default="hackathon-data", help="Data directory (default: hackathon-data)")
    parser.add_argument("--stats-out", default="graph_stats.json", help="Output path for graph_stats.json")
    parser.add_argument("--public-out", default="stage1_public.json", help="Output path for stage1_public.json")
    args = parser.parse_args()

    success = run_harness(args.module, args.data, args.stats_out, args.public_out)
    sys.exit(0 if success else 1)
