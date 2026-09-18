"""
demo.py
Interactive Demonstration Tool for Study Sentinel — Stage 1 (ATLAS).

Designed for live judge demos, screen recordings, and prototype walkthroughs.
Imports existing stage1.atlas and starter.schemas without any backend modifications.
"""

import sys
import os
import json
import time

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from stage1.atlas import StudyGraph, Atlas
from starter.schemas import Question, Answer

def print_header(title: str):
    print("\n" + "=" * 72)
    print(f"  {title.upper()}")
    print("=" * 72)

def print_box(label: str, content: str):
    print(f"\n--- {label} ---")
    print(content)

def main():
    print_header("Study Sentinel — Stage 1 (ATLAS) Prototype")
    print("Initializing in-memory Clinical Knowledge Graph...")
    
    data_dir = "hackathon-data"
    if not os.path.exists(data_dir):
        print(f"Error: Data directory '{data_dir}' not found.")
        return

    t0 = time.perf_counter()
    graph = StudyGraph(data_dir)
    stats = graph.build()
    build_sec = time.perf_counter() - t0

    atlas = Atlas(graph)

    print(f"Graph loaded in {build_sec:.2f} seconds.")
    print(f"  Total Nodes Indexed:    {stats.get('nodes'):,}")
    print(f"  Total Relations/Edges:  {stats.get('edges'):,}")
    print(f"  Total Subjects Covered: {stats.get('subjects')}")
    print(f"  Active Protocol Ver:    v{graph.protocol_version}")

    demo_questions = [
        ("Hy's Law Liver Safety Adjudication (with S07 Unit Conversion)", 
         Question("DEMO-01", "Which subjects meet potential Hy's law criteria?")),
        ("Adversarial Trap Query (Site S01 Dosing Errors)", 
         Question("DEMO-02", "Which subjects at site S01 received a wrong dose?", kind="trap")),
        ("Visit Window Lookup (042-S05-003 around WEEK8)", 
         Question("DEMO-03", "List the laboratory and adverse-event records for 042-S05-003 within 7 days of the WEEK8 visit")),
        ("Site Discontinuation Count (Site S07 Discontinuations)", 
         Question("DEMO-04", "How many subjects at site S07 discontinued due to an adverse event?", kind="count")),
        ("Prohibited Medications (Protocol v1-v3 Compliance)", 
         Question("DEMO-05", "Which subjects took a prohibited systemic glucocorticoid concomitant medication?")),
        ("Miscoded Serious Adverse Event Detection (Hospitalization Override)", 
         Question("DEMO-06", "Which subjects experienced a miscoded serious adverse event (hospitalized with AESER=N)?")),
        ("Duplicate Subject Identification Across Different Sites", 
         Question("DEMO-07", "Which subjects are duplicate enrollments across multiple sites?")),
    ]

    while True:
        print_header("Demonstration Menu")
        for idx, (desc, _) in enumerate(demo_questions, 1):
            print(f"  [{idx}] {desc}")
        print(f"  [8] Inspect Patient 360 Profile for a Subject (e.g. 042-S07-001)")
        print(f"  [9] Run Full 10-Question Benchmark Suite")
        print(f"  [0] Exit")

        try:
            choice = input("\nSelect an option [0-9] (or press Enter for 1): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting ATLAS Prototype. Goodbye!")
            break
        if not choice:
            choice = "1"

        if choice == "0":
            print("\nExiting ATLAS Prototype. Goodbye!")
            break

        elif choice in [str(i) for i in range(1, 8)]:
            q_idx = int(choice) - 1
            desc, q = demo_questions[q_idx]
            print_header(f"Query Execution: {desc}")
            print(f"Question ID: {q.question_id}")
            print(f"Text:        \"{q.text}\"")

            t_query = time.perf_counter()
            ans: Answer = atlas.answer(q)
            q_elapsed = (time.perf_counter() - t_query) * 1000

            print_box("Calculated Answer", json.dumps(ans.answer, indent=2))
            print_box("Clinical Explanation", ans.text)
            print_box("Supporting Evidence (Exact Records)", json.dumps([e.to_dict() if hasattr(e, "to_dict") else e for e in ans.evidence], indent=2))
            print(f"\nConfidence:  {ans.confidence:.2f}")
            print(f"Query Time:  {q_elapsed:.2f} ms (< 120,000 ms limit)")

        elif choice == "8":
            default_subj = "042-S07-001"
            subj_input = input(f"\nEnter Subject ID to inspect [{default_subj}]: ").strip()
            if not subj_input:
                subj_input = default_subj

            p360 = graph.patient360(subj_input)
            if "error" in p360:
                print(f"\nSubject '{subj_input}' not found in study graph.")
                continue

            print_header(f"Patient 360 View — Subject: {subj_input}")
            print(f"Site ID:          {p360.get('siteid')}")
            print(f"Demographics:     Age {p360['demographics'].get('AGE')}, Sex {p360['demographics'].get('SEX')}, Arm {p360['demographics'].get('ARM')}")
            print(f"Total Labs:       {len(p360.get('laboratory', []))}")
            print(f"Total AEs:        {len(p360.get('adverse_events', []))}")
            print(f"Total Doses:      {len(p360.get('exposure', []))}")
            print(f"Total ConMeds:    {len(p360.get('concomitant_medications', []))}")
            print(f"Visits Recorded:  {list(p360.get('visits', {}).keys())}")
            
            print("\nClinical Safety Signals:")
            signals = p360.get("signals", {})
            print(f"  - Hy's Law Liver Signal:  {signals.get('hys_law')} ({len(signals.get('hys_law_records', []))} qualifying records)")
            print(f"  - Dosing Deviations:      {len(signals.get('dosing_errors', []))}")
            print(f"  - Prohibited Medications: {len(signals.get('prohibited_meds', []))}")
            print(f"  - Serious AEs (SAE):      {len(signals.get('sae', []))}")
            print(f"  - Miscoded SAEs:          {len(signals.get('miscoded_sae', []))}")

        elif choice == "9":
            print_header("Executing Benchmark Suite")
            os.system(f"{sys.executable} starter/run_local_harness.py --module stage1.atlas --data hackathon-data")

        else:
            print("Invalid selection. Please enter a number between 0 and 9.")

        try:
            input("\nPress Enter to return to menu...")
        except (EOFError, KeyboardInterrupt):
            print("\nExiting ATLAS Prototype. Goodbye!")
            break

if __name__ == "__main__":
    main()
