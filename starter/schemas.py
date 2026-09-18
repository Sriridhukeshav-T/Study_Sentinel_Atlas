"""
starter/schemas.py
Standard schema definitions for Study Sentinel - Stage 1 (ATLAS).
Contract models for questions, answers, and supporting evidence references.
DO NOT MODIFY in production grading environments.
"""

from dataclasses import dataclass, field, asdict
from typing import Any, List, Optional, Union

@dataclass
class RecordRef:
    """
    Identifies a specific clinical record or document citation as evidence.
    For clinical domains: (domain, usubjid, seq) e.g. RecordRef(domain="LB", usubjid="042-S07-001", seq=25)
    For protocol/manual: (domain="DOC", document="lab-manual", section="units")
    """
    domain: str
    usubjid: Optional[str] = None
    seq: Optional[int] = None
    document: Optional[str] = None
    section: Optional[str] = None

    def to_dict(self) -> dict:
        d = {"domain": self.domain}
        if self.usubjid is not None:
            d["usubjid"] = self.usubjid
        if self.seq is not None:
            d["seq"] = int(self.seq)
        if self.document is not None:
            d["document"] = self.document
        if self.section is not None:
            d["section"] = self.section
        return d

    def __hash__(self):
        return hash((self.domain, self.usubjid, self.seq, self.document, self.section))

    def __eq__(self, other):
        if not isinstance(other, (RecordRef, dict)):
            return False
        if isinstance(other, dict):
            return self.to_dict() == other
        return (
            self.domain == other.domain
            and self.usubjid == other.usubjid
            and self.seq == other.seq
            and self.document == other.document
            and self.section == other.section
        )


@dataclass
class Question:
    """
    Standard Question schema submitted to the Atlas agent.
    """
    question_id: str
    text: str
    kind: Optional[str] = None  # count, lookup, finding, trap
    cut: Optional[int] = None
    metadata: Optional[dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        res = {
            "question_id": self.question_id,
            "text": self.text,
        }
        if self.kind is not None:
            res["kind"] = self.kind
        if self.cut is not None:
            res["cut"] = self.cut
        if self.metadata:
            res["metadata"] = self.metadata
        return res


@dataclass
class Answer:
    """
    Standard Answer schema returned by Atlas.answer(question).
    """
    question_id: str
    answer: Any  # int, list of subject IDs, list of record dicts, or []
    text: str
    evidence: List[Union[RecordRef, dict]] = field(default_factory=list)
    confidence: float = 1.0
    steps_used: int = 0
    tokens_used: int = 0

    def to_dict(self) -> dict:
        ev_list = []
        for ev in self.evidence:
            if hasattr(ev, "to_dict"):
                ev_list.append(ev.to_dict())
            elif isinstance(ev, dict):
                ev_list.append(ev)
            else:
                ev_list.append(str(ev))
        return {
            "question_id": self.question_id,
            "answer": self.answer,
            "text": self.text,
            "evidence": ev_list,
            "confidence": round(float(self.confidence), 4),
            "steps_used": int(self.steps_used),
            "tokens_used": int(self.tokens_used),
        }
