import json

from code2req.agents.module_agent import parse_requirements
from code2req.dedup import assign_ids, dedup
from code2req.schema import Evidence, FlatRequirement
from code2req.structure import Module

# Field names of docreq's flatRequirement (types.go json tags).
DOCREQ_KEYS = {
    "id", "text", "section", "type", "canonical_name", "name", "group_id", "attributes",
    "actors", "parent_id", "parent_relation", "subject_name", "subject_multiplicity",
    "claim_attribute", "claim_value", "page", "source_file",
}


def test_json_carries_every_docreq_key():
    data = FlatRequirement(text="The system shall X.", source_name="Title").to_json()
    assert DOCREQ_KEYS <= set(data)
    assert data["name"] == "Title"
    assert json.loads(json.dumps(data))["text"] == "The system shall X."


def test_parse_requirements_sets_section_and_repairs_fields():
    module = Module(name="cart", path=["shop", "cart"])
    raw = '```json\n{"requirements": [' \
          '{"id": "X", "text": "The API shall reject zero quantities.", "type": "weird",' \
          ' "confidence": "sure", "evidence": [{"file": "a.py", "start_line": 1, "end_line": 2}]},' \
          '{"text": ""}, "junk"]}\n```'
    reqs = parse_requirements(raw, module)
    assert len(reqs) == 1
    r = reqs[0]
    assert (r.section, r.module, r.type, r.confidence, r.id) == \
        ("shop > cart", "cart", "functional", "inferred", "")


def test_parse_requirements_bad_json_returns_empty():
    assert parse_requirements("not json", Module(name="m", path=["m"])) == []


def mk(text, claim=("", ""), conf="inferred", line=1, section="s"):
    return FlatRequirement(text=text, section=section, module="cart", confidence=conf,
                           claim_attribute=claim[0], claim_value=claim[1],
                           evidence=[Evidence(file="a.py", start_line=line, end_line=line)])


def test_dedup_exact_text_and_claim_key_merge_evidence_and_confidence():
    reqs = [
        mk("The system shall X.", line=1),
        mk("the system  shall x", conf="confirmed", line=2),
        mk("Cart holds at most 50 items.", claim=("cart_items", "max_50"), line=3),
        mk("The cart shall not exceed 50 items.", claim=("cart_items", "max_50"), line=4),
        mk("Other section same claim.", claim=("cart_items", "max_50"), section="t"),
    ]
    out, dropped = dedup(reqs)
    assert dropped == 2
    assert len(out) == 3
    assert out[0].confidence == "confirmed"
    assert [e.start_line for e in out[0].evidence] == [1, 2]
    assert [e.start_line for e in out[1].evidence] == [3, 4]


def test_assign_ids_per_module():
    reqs = [mk("a"), mk("b")]
    reqs[1].module = "billing (part 2)"
    assign_ids(reqs)
    assert [r.id for r in reqs] == ["CODE-CART-001", "CODE-BILLING-001"]
    assert reqs[0].group_id == "code_cart_001"
