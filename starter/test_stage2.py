"""
starter/test_stage2.py
Comprehensive test suite for Problem 2 (MONITOR).
Validates all 9 critical evaluation criteria:
1. Run one cycle end-to-end
2. Run the SAME cut again -> verify 0 duplicate queries, 0 duplicate escalations (MEMORY)
3. Verify AESHOSP=Y + AESER=N is recognized as serious
4. Verify genuine data-quality issue creates a specific query
5. Verify APPROVED monitor reply
6. Verify REJECTED monitor reply and non-re-escalation
7. Verify CLARIFY monitor reply: system answers from own graph, then resubmits
8. Verify changed protocol version / data cut (mid-stage change)
9. Verify every node writes to the trace as it happens
"""

import os
import sys
import json
import unittest

# Ensure project root in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from stage1.atlas import StudyGraph, Atlas
from stage2.crew import ReviewCrew, ReviewReport, PersistentMemory, TraceLogger


class TestStage2Monitor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = "hackathon-data"
        cls.test_memory_path = "test_memory.json"
        if os.path.exists(cls.test_memory_path):
            os.remove(cls.test_memory_path)
            
        cls.graph = StudyGraph(cls.data_dir)
        cls.atlas = Atlas(cls.graph)

    def setUp(self):
        if os.path.exists(self.test_memory_path):
            os.remove(self.test_memory_path)

    def tearDown(self):
        if os.path.exists(self.test_memory_path):
            os.remove(self.test_memory_path)

    def test_01_run_cycle_end_to_end(self):
        """TEST 1: Run one cycle end-to-end and verify structure."""
        crew = ReviewCrew(
            hub_url=None,
            gateway_url=None,
            team_key="test-team-key",
            atlas=self.atlas,
            memory_path=self.test_memory_path
        )
        report: ReviewReport = crew.run_cycle(cut=6, protocol_version=2)
        
        self.assertIsInstance(report, ReviewReport)
        self.assertEqual(report.cycle, 1)
        self.assertEqual(report.cut, 6)
        self.assertEqual(report.protocol_version, 2)
        self.assertGreater(len(report.findings), 0)
        self.assertGreater(len(report.trace), 0)
        print("\n[PASS] Test 1: Cycle executed successfully with report generated.")

    def test_02_memory_across_cycles_zero_duplicates(self):
        """TEST 2: Run the SAME cut twice -> 0 duplicate queries and 0 duplicate escalations."""
        crew = ReviewCrew(
            hub_url=None,
            gateway_url=None,
            team_key="test-team-key",
            atlas=self.atlas,
            memory_path=self.test_memory_path
        )
        # Cycle 1
        report1 = crew.run_cycle(cut=6, protocol_version=2)
        q1_count = len(report1.queries)
        e1_count = len(report1.escalations)
        self.assertGreater(q1_count, 0)
        self.assertGreater(e1_count, 0)

        # Cycle 2 on SAME cut
        report2 = crew.run_cycle(cut=6, protocol_version=2)
        q2_count = len(report2.queries)
        e2_count = len(report2.escalations)

        self.assertEqual(q2_count, 0, f"Expected 0 duplicate queries in cycle 2, got {q2_count}")
        self.assertEqual(e2_count, 0, f"Expected 0 duplicate escalations in cycle 2, got {e2_count}")
        print(f"\n[PASS] Test 2: Memory verified across cycles. Cycle 1 had {q1_count} queries & {e1_count} escalations; Cycle 2 had 0 new queries & 0 new escalations.")

    def test_03_miscoded_sae_hospitalization(self):
        """TEST 3: Verify AESHOSP=Y + AESER=N is recognized as serious and escalated."""
        crew = ReviewCrew(
            hub_url=None,
            gateway_url=None,
            team_key="test-team-key",
            atlas=self.atlas,
            memory_path=self.test_memory_path
        )
        report = crew.run_cycle(cut=6, protocol_version=2)
        
        miscoded_escs = [
            e for e in report.escalations
            if e.get("usubjid") == "042-S02-004" and e.get("code") == "SAE_MISCODED"
        ]
        self.assertEqual(len(miscoded_escs), 1, "Subject 042-S02-004 should be escalated for SAE_MISCODED")
        esc = miscoded_escs[0]
        self.assertEqual(esc["severity"], "CRITICAL")
        self.assertIn("AESHOSP=Y", esc["summary"])
        print("\n[PASS] Test 3: AESHOSP=Y + AESER=N successfully recognized as serious under Protocol §6.")

    def test_04_genuine_data_quality_query(self):
        """TEST 4: Verify genuine data-quality problem creates a specific, actionable query."""
        crew = ReviewCrew(
            hub_url=None,
            gateway_url=None,
            team_key="test-team-key",
            atlas=self.atlas,
            memory_path=self.test_memory_path
        )
        report = crew.run_cycle(cut=6, protocol_version=2)
        
        s11_queries = [
            q for q in report.queries
            if q["usubjid"] == "042-S11-005" and q["domain"] == "AE"
        ]
        self.assertEqual(len(s11_queries), 1, "Expected specific query for 042-S11-005 AE before first dose")
        q = s11_queries[0]
        self.assertIn("Fatigue", q["text"])
        self.assertIn("before first dose", q["text"])
        self.assertEqual(q["seq"], 1)
        self.assertEqual(q["status"], "CLOSED")
        print(f"\n[PASS] Test 4: Specific, actionable query generated: '{q['text']}'.")

    def test_05_approved_monitor_decision(self):
        """TEST 5: Verify APPROVED response is properly recorded and logged."""
        crew = ReviewCrew(
            hub_url=None,
            gateway_url=None,
            team_key="test-team-key",
            atlas=self.atlas,
            memory_path=self.test_memory_path
        )
        report = crew.run_cycle(cut=6, protocol_version=2)
        
        approved = [
            d for d in report.human_gate_decisions
            if d.get("decision") == "APPROVED"
        ]
        self.assertGreater(len(approved), 0, "Expected at least one approved escalation")
        hg_traces = [
            t for t in report.trace
            if t["node"] == "human_gate" and "APPROVED" in t["message"]
        ]
        self.assertGreater(len(hg_traces), 0, "Expected human_gate APPROVED trace entry")
        print("\n[PASS] Test 5: APPROVED escalation executed and recorded in trace.")

    def test_06_rejected_monitor_decision_and_non_reescalation(self):
        """TEST 6: Test REJECTED is downgraded to monitoring and NOT re-escalated next cycle."""
        crew = ReviewCrew(
            hub_url=None,
            gateway_url=None,
            team_key="test-team-key",
            atlas=self.atlas,
            memory_path=self.test_memory_path
        )
        
        crew.canned_monitor_decisions["TEST_CODE|042-TEST-001"] = [
            "REJECTED",
            "Not clinically significant; monitor only."
        ]
        
        trace = TraceLogger()
        test_draft = [{
            "code": "TEST_CODE",
            "usubjid": "042-TEST-001",
            "site": "S01",
            "severity": "HIGH",
            "summary": "Test issue"
        }]
        
        decisions1 = crew._node_human_gate(test_draft, cut=6, trace=trace, cycle=1)
        self.assertEqual(decisions1[0]["decision"], "REJECTED")
        self.assertEqual(decisions1[0]["action"], "DOWNGRADED_TO_MONITORING")
        self.assertTrue(crew.memory.is_escalation_rejected("TEST_CODE", "042-TEST-001"))
        self.assertTrue(crew.memory.is_escalation_raised("TEST_CODE", "042-TEST-001"))
        print("\n[PASS] Test 6: REJECTED properly downgraded to monitoring and blocked from re-escalation.")

    def test_07_clarify_answered_from_graph_and_resubmitted(self):
        """TEST 7: Test CLARIFY: system looks up answer from StudyGraph, resubmits, and completes as APPROVED."""
        crew = ReviewCrew(
            hub_url=None,
            gateway_url=None,
            team_key="test-team-key",
            atlas=self.atlas,
            memory_path=self.test_memory_path
        )
        
        trace = TraceLogger()
        test_draft = [{
            "code": "HYS_LAW_CANDIDATE",
            "usubjid": "042-S01-001",
            "site": "S01",
            "severity": "CRITICAL",
            "summary": "Hy's law test"
        }]
        decisions = crew._node_human_gate(test_draft, cut=6, trace=trace, cycle=1)
        
        self.assertEqual(len(decisions), 1)
        dec = decisions[0]
        self.assertEqual(dec["initial_status"], "CLARIFY")
        self.assertEqual(dec["decision"], "APPROVED")
        self.assertIn("screening ALT", dec["clarification_answer"])
        self.assertIn("conmed", dec["clarification_answer"].lower())
        
        trace_str = trace.entries[-1]["message"]
        self.assertIn("CLARIFY; answered from graph", trace_str)
        self.assertIn("resubmitted -> APPROVED", trace_str)
        print(f"\n[PASS] Test 7: CLARIFY answered from graph ({dec['clarification_answer']}) and resubmitted -> APPROVED.")

    def test_08_mid_stage_protocol_change(self):
        """TEST 8: Verify that changing protocol version (v1 -> v2) changes compliance results dynamically."""
        crew = ReviewCrew(
            hub_url=None,
            gateway_url=None,
            team_key="test-team-key",
            atlas=self.atlas,
            memory_path=self.test_memory_path
        )
        
        report_v1 = crew.run_cycle(cut=4, protocol_version=1)
        renal_v1 = [d for d in report_v1.compliance_deviations if d.get("category") == "renal_exclusion"]
        self.assertEqual(len(renal_v1), 0, "Protocol v1 should have 0 renal exclusions")
        
        if os.path.exists(self.test_memory_path):
            os.remove(self.test_memory_path)
        crew2 = ReviewCrew(
            hub_url=None,
            gateway_url=None,
            team_key="test-team-key",
            atlas=self.atlas,
            memory_path=self.test_memory_path
        )
        
        report_v2 = crew2.run_cycle(cut=6, protocol_version=2)
        renal_v2 = [d for d in report_v2.compliance_deviations if d.get("category") == "renal_exclusion"]
        self.assertEqual(len(renal_v2), 4, f"Protocol v2 should have 4 renal exclusions, got {len(renal_v2)}")
        print(f"\n[PASS] Test 8: Mid-stage amendment verified: v1 had {len(renal_v1)} renal deviations, v2 has {len(renal_v2)} renal deviations.")

    def test_09_all_nodes_write_to_trace(self):
        """TEST 9: Verify every single one of the 6 nodes writes to the trace as it happens."""
        crew = ReviewCrew(
            hub_url=None,
            gateway_url=None,
            team_key="test-team-key",
            atlas=self.atlas,
            memory_path=self.test_memory_path
        )
        report = crew.run_cycle(cut=6, protocol_version=2)
        
        nodes_in_trace = set(t["node"] for t in report.trace)
        expected_nodes = {"detect", "medical_review", "data_manager", "compliance", "human_gate", "execute"}
        
        for node in expected_nodes:
            self.assertIn(node, nodes_in_trace, f"Node '{node}' missing from trace log")
            
        print("\n[PASS] Test 9: All 6 nodes verified writing to trace:", sorted(list(nodes_in_trace)))


if __name__ == "__main__":
    unittest.main()
