"""Output models.

FlatRequirement mirrors docreq's flatRequirement JSON tags
(sysmodeler-backend-ai/app/chatbot/tools/docreq/types.go) so the emitted
requirements.json can be unmarshalled by Go without a mapping layer. The
extra keys (evidence, confidence, module) are ignored by Go's json package.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Confidence = Literal["confirmed", "inferred", "gap"]

REQUIREMENT_TYPES = (
    "functional",
    "performance",
    "interface",
    "safety",
    "constraint",
    "design",
    "physical",
)


class Evidence(BaseModel):
    """A code location that supports a requirement."""

    file: str  # repo-relative, forward slashes
    start_line: int
    end_line: int
    symbol: str = ""  # function/class/constant the claim depends on
    ok: bool | None = None  # set by verify
    note: str = ""  # why verification failed


class Attribute(BaseModel):
    """One quantitative threshold. Same shape as docreq's llmAttribute."""

    name: str
    isq_type: str = "ScalarValues::Real"
    operator: str = "=="
    value: float = 0.0
    unit_literal: str = ""
    multiplicity: str = ""


class Actor(BaseModel):
    name: str
    type: str = ""


class FlatRequirement(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = ""
    text: str
    section: str = "General"
    type: str = "functional"
    canonical_name: str = ""
    source_name: str = Field(default="", alias="name")
    group_id: str = ""
    attributes: list[Attribute] = Field(default_factory=list)
    actors: list[Actor] = Field(default_factory=list)
    parent_id: str = ""
    parent_relation: str = ""
    subject_name: str = ""
    subject_multiplicity: str = ""
    claim_attribute: str = ""
    claim_value: str = ""
    page: str = ""
    source_file: str = ""

    # code2req-only keys
    module: str = ""
    confidence: Confidence = "inferred"
    evidence: list[Evidence] = Field(default_factory=list)

    def to_json(self) -> dict:
        return self.model_dump(by_alias=True)


class Surface(BaseModel):
    """An observable entry point or rule found deterministically in the code."""

    kind: Literal["http_route", "cli_command", "env_var", "constant", "test", "model"]
    name: str
    file: str
    line: int
    end_line: int
    detail: str = ""


class RunReport(BaseModel):
    requirements: list[FlatRequirement] = Field(default_factory=list)
    gaps: list[FlatRequirement] = Field(default_factory=list)
    rejected: list[FlatRequirement] = Field(default_factory=list)
    surfaces: list[Surface] = Field(default_factory=list)
    stats: dict = Field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "requirements": [r.to_json() for r in self.requirements],
            "gaps": [r.to_json() for r in self.gaps],
            "rejected": [r.to_json() for r in self.rejected],
            "surfaces": [s.model_dump() for s in self.surfaces],
            "stats": self.stats,
        }
