"""End-to-end on the fixture repo with a fake LLM (real CodeWiki parsing)."""

import json
import os

from code2req.pipeline import run
from code2req.prompts import MODULE_SYSTEM

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "shop")

CLAIMS = {"requirements": [
    {"text": "The API shall reject cart items whose quantity is less than 1 with status 422.",
     "type": "constraint", "confidence": "confirmed", "claim_attribute": "cart_item_quantity",
     "claim_value": "min_1",
     "evidence": [{"file": "app/api.py", "start_line": 18, "end_line": 20,
                   "symbol": "validate_quantity"},
                  {"file": "tests/test_api.py", "start_line": 7, "end_line": 10,
                   "symbol": "validate_quantity"}]},
    {"text": "The API shall hold at most 50 items in a cart.", "type": "performance",
     "confidence": "inferred",
     "attributes": [{"name": "cartItems", "operator": "<=", "value": 50}],
     "evidence": [{"file": "app/api.py", "start_line": 24, "end_line": 29, "symbol": "add_item"}]},
    {"text": "The API shall return an order by id.", "type": "interface", "confidence": "gap",
     "evidence": [{"file": "app/api.py", "start_line": 33, "end_line": 35, "symbol": "get_order"}]},
    {"text": "The system shall email receipts.", "confidence": "inferred",
     "evidence": [{"file": "app/mail.py", "start_line": 1, "end_line": 3}]},
]}


class FixtureLLM:
    def __init__(self):
        self.prompts = []

    def complete(self, system, user, json_mode=False):
        self.prompts.append((system, user))
        assert system == MODULE_SYSTEM, "small repo must not trigger LLM clustering"
        return json.dumps(CLAIMS)


class PartialThenFollowupLLM:
    """First pass cites only validate_quantity; the follow-up covers add_item."""

    def __init__(self):
        self.calls = []

    def complete(self, system, user, json_mode=False):
        from code2req.prompts import FOLLOWUP_SYSTEM
        self.calls.append((system, user))
        if system == FOLLOWUP_SYSTEM:
            return json.dumps({"requirements": [
                {"text": "The API shall refuse to add an item when the cart already holds 50 items.",
                 "type": "constraint",
                 "evidence": [{"file": "app/api.py", "start_line": 24, "end_line": 29, "symbol": "add_item"}]}],
                "internal": []})
        return json.dumps({"requirements": [CLAIMS["requirements"][0]]})


def test_followup_covers_uncited_functions(tmp_path):
    llm = PartialThenFollowupLLM()
    report = run(FIXTURE, str(tmp_path), llm, "fake/model", workers=1)
    from code2req.prompts import FOLLOWUP_SYSTEM
    followups = [u for s, u in llm.calls if s == FOLLOWUP_SYSTEM]
    assert len(followups) == 1
    assert "add_item" in followups[0] and "def add_item" in followups[0]
    assert "REQUIREMENTS ALREADY WRITTEN" in followups[0]
    assert any("50 items" in r.text for r in report.requirements)
    fc = report.stats["function_coverage"]
    # functions: validate_quantity (cited in pass 1), add_item (cited by the follow-up),
    # get_order (shown to the follow-up, which wrote nothing for it)
    assert (fc["total"], fc["cited_first_pass"], fc["cited"]) == (3, 1, 2)
    assert fc["uncited"] == ["app/api.py:33 get_order"]


def test_followup_can_be_disabled(tmp_path):
    llm = PartialThenFollowupLLM()
    report = run(FIXTURE, str(tmp_path), llm, "fake/model", workers=1, followup=False)
    assert len(llm.calls) == 1
    assert report.stats["function_coverage"]["uncited"] == ["app/api.py:24 add_item",
                                                            "app/api.py:33 get_order"]


def test_end_to_end(tmp_path):
    llm = FixtureLLM()
    report = run(FIXTURE, str(tmp_path), llm, "fake/model", workers=1)

    # Test code is not sent as module code, but the test is attached to the module.
    assert len(llm.prompts) == 1
    prompt = llm.prompts[0][1]
    assert "app/api.py" in prompt
    assert "TESTS THAT EXERCISE THIS MODULE" in prompt
    assert "test_validate_quantity_rejects_zero" in prompt
    assert "[http_route] POST /cart/items" in prompt

    texts = {r.text: r for r in report.requirements}
    assert len(report.requirements) == 2
    q = texts["The API shall reject cart items whose quantity is less than 1 with status 422."]
    assert q.confidence == "confirmed" and q.id.startswith("CODE-")
    assert [g.text for g in report.gaps] == ["The API shall return an order by id."]
    assert [r.text for r in report.rejected] == ["The system shall email receipts."]

    cov = report.stats["surface_coverage"]
    assert cov["by_kind"]["http_route"] == {"total": 2, "covered": 1}
    # add_item's cited body uses MAX_CART_ITEMS, so the constant counts as covered.
    assert cov["by_kind"]["constant"] == {"total": 2, "covered": 1}

    data = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    assert len(data["requirements"]) == 2
    assert "Rejected claims" in (tmp_path / "requirements.md").read_text(encoding="utf-8")
