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
        from code2req.prompts import FOLLOWUP_SYSTEM
        self.prompts.append((system, user))
        assert system in (MODULE_SYSTEM, FOLLOWUP_SYSTEM), "small repo must not trigger LLM clustering"
        if system == FOLLOWUP_SYSTEM:
            return json.dumps({"requirements": [], "internal": []})
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


class AssetsAndDeadCodeLLM:
    """Module pass also claims something about dead code; the asset pass covers the prompt and
    the reference file."""

    def __init__(self):
        self.calls = []

    def complete(self, system, user, json_mode=False):
        from code2req.prompts import ASSET_SYSTEM, FOLLOWUP_SYSTEM
        self.calls.append((system, user))
        if system == ASSET_SYSTEM:
            return json.dumps({
                "requirements": [{"text": "The receipt assistant shall list each item with its quantity.",
                                  "type": "interface", "evidence": [{"file": "app/prompts/receipt.txt",
                                  "start_line": 2, "end_line": 2, "symbol": "each item with its quantity"}]}],
                "references": [{"text": "The knowledge base shall provide reference material on cart rules.",
                                "type": "design", "evidence": [{"file": "app/knowledge/cart.md",
                                "start_line": 1, "end_line": 2, "symbol": "Cart rules"}]}]})
        if system == FOLLOWUP_SYSTEM:
            return json.dumps({"requirements": [], "internal": []})
        return json.dumps({"requirements": CLAIMS["requirements"][:2] + [
            {"text": "The system shall compute legacy discounts.", "type": "functional",
             "evidence": [{"file": "app/legacy.py", "start_line": 1, "end_line": 2, "symbol": "discount"}]},
            {"text": "The system shall format receipts as plain text.", "type": "functional",
             "evidence": [{"file": "app/receipts.py", "start_line": 11, "end_line": 12, "symbol": "unused_format"}]},
        ]})


def test_assets_references_and_dead_code(tmp_path):
    import shutil
    repo = tmp_path / "shop"
    shutil.copytree(FIXTURE, repo, ignore=shutil.ignore_patterns("__pycache__"))
    (repo / "app" / "prompts").mkdir()
    (repo / "app" / "prompts" / "receipt.txt").write_text("You write receipts.\nList each item with its quantity.\n")
    (repo / "app" / "knowledge").mkdir()
    (repo / "app" / "knowledge" / "cart.md").write_text("# Cart rules\nA cart holds items.\n")
    (repo / "app" / "receipts.py").write_text(
        "from pathlib import Path\n"
        "PROMPTS_DIR = Path(__file__).parent / 'prompts'\n"
        "KNOWLEDGE_DIR = Path(__file__).parent / 'knowledge'\n"
        "__all__ = ['receipt_prompt']\n\n"
        "def receipt_prompt():\n"
        "    text = (PROMPTS_DIR / 'receipt.txt').read_text()\n"
        "    return text, KNOWLEDGE_DIR\n\n\n"
        "def unused_format(items):\n"
        "    return ', '.join(items)\n")
    (repo / "app" / "legacy.py").write_text("def discount(x):\n    return x * 0.9\n")
    api = (repo / "app" / "api.py").read_text().replace("import os\n", "import os\n\nfrom app import receipts\n", 1)
    (repo / "app" / "api.py").write_text(api)
    # Two lines were added at the top of api.py: shift the fixture claims to match.
    for c in CLAIMS["requirements"][:2]:
        for e in c["evidence"]:
            if e["file"] == "app/api.py":
                e["start_line"] += 2
                e["end_line"] += 2
    try:
        llm = AssetsAndDeadCodeLLM()
        report = run(str(repo), str(tmp_path / "out"), llm, "fake/model", workers=1)
    finally:
        for c in CLAIMS["requirements"][:2]:
            for e in c["evidence"]:
                if e["file"] == "app/api.py":
                    e["start_line"] -= 2
                    e["end_line"] -= 2

    from code2req.prompts import ASSET_SYSTEM, MODULE_SYSTEM
    module_prompts = [u for s, u in llm.calls if s == MODULE_SYSTEM]
    asset_prompts = [u for s, u in llm.calls if s == ASSET_SYSTEM]
    # legacy.py is imported by nothing: it never reaches the model.
    assert all("app/legacy.py" not in p for p in module_prompts)
    assert any("UNUSED CODE" in p and "unused_format" in p for p in module_prompts)
    # app/prompts and app/knowledge are separate asset groups; each names the code loading it:
    # receipt.txt by file name, the knowledge folder through the KNOWLEDGE_DIR constant.
    assert len(asset_prompts) == 2
    loaders = {p.split("\n")[0]: p.split("LOADED BY:\n- ")[1].split(":")[1] for p in asset_prompts}
    assert loaders == {"ASSET GROUP: assets > app/prompts": "7", "ASSET GROUP: assets > app/knowledge": "8"}

    texts = [r.text for r in report.requirements]
    assert "The receipt assistant shall list each item with its quantity." in texts
    prompt_req = next(r for r in report.requirements if r.asset_role)
    assert prompt_req.asset_role == "prompt" and prompt_req.id.startswith("CODE-")
    assert [(r.id, r.asset_role) for r in report.references] == [("REF-KNOWLEDGE-001", "reference")]
    # Claims about dead code are set aside with the reason, never mixed into the requirements.
    dead = {r.text: r.dead_reason for r in report.dead_requirements}
    assert dead == {"The system shall compute legacy discounts.": "file is not imported from any entry point",
                    "The system shall format receipts as plain text.": "function is never referenced by live code"}
    assert not any("legacy" in t or "plain text" in t for t in texts)
    assert {(d.kind, d.file, d.name) for d in report.dead_code} == {
        ("file", "app/legacy.py", ""), ("function", "app/receipts.py", "unused_format")}

    st = report.stats
    assert st["asset_coverage"]["by_role"] == {"prompt": {"files": 1, "cited": 1},
                                               "reference": {"files": 1, "cited": 1}}
    assert st["reachability"]["dead_files"] == 1 and st["function_coverage"]["dead_excluded"] == 1
    md = (tmp_path / "out" / "requirements.md").read_text(encoding="utf-8")
    assert "## Reference material" in md and "## Dead code (no requirements written)" in md
    data = json.loads((tmp_path / "out" / "requirements.json").read_text(encoding="utf-8"))
    assert data["references"][0]["id"] == "REF-KNOWLEDGE-001" and data["dead_code"]
    assert (tmp_path / "out" / "requirements.pdf").read_bytes().startswith(b"%PDF")


def test_end_to_end(tmp_path):
    llm = FixtureLLM()
    report = run(FIXTURE, str(tmp_path), llm, "fake/model", workers=1)

    # Test code is not sent as module code, but the test is attached to the module.
    module_prompts = [u for s, u in llm.prompts if s == MODULE_SYSTEM]
    followups = [u for s, u in llm.prompts if s != MODULE_SYSTEM]
    assert len(module_prompts) == 1
    # Every function is cited, but the CartItem model is not: the follow-up asks about it.
    assert len(followups) == 1 and "CartItem" in followups[0]
    assert report.stats["type_coverage"]["uncited"] == ["app/api.py:13 CartItem"]
    prompt = module_prompts[0]
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
    # GET /orders/{order_id} is covered by the gap: gaps count towards surface coverage.
    assert cov["by_kind"]["http_route"] == {"total": 2, "covered": 2}
    # add_item's cited body uses MAX_CART_ITEMS, so the constant counts as covered.
    assert cov["by_kind"]["constant"] == {"total": 2, "covered": 1}

    data = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    assert len(data["requirements"]) == 2
    assert "Rejected claims" in (tmp_path / "requirements.md").read_text(encoding="utf-8")
