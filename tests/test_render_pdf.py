"""The PDF report renders any requirements.json: current, older (missing keys), empty, and
text that would be markup if the template ever read it as such."""

from code2req.render_pdf import render_pdf


def req(i, text, **extra):
    return {"id": f"CODE-API-{i:03d}", "text": text, "section": "app/api", "type": "functional",
            "confidence": "inferred", "canonical_name": "Cart Limit",
            "evidence": [{"file": "app/api.py", "start_line": 24, "end_line": 29},
                         {"file": "app/api.py", "start_line": 40, "end_line": 40}], **extra}


def test_renders_a_current_result_with_every_part():
    result = {
        "requirements": [req(1, "The API shall hold at most 50 items in a `Cart`.",
                             attributes=[{"name": "cartItems", "operator": "<=", "value": 50}],
                             review={"verdict": "accepted", "note": "checked"}),
                         req(2, "The receipt prompt shall list items.", asset_role="prompt")],
        "references": [req(3, "The knowledge base shall describe carts.", asset_role="reference")],
        "gaps": [req(4, "The API shall return an order.", confidence="gap")],
        "rejected": [{"text": "The system shall email receipts.", "evidence": [
            {"file": "app/mail.py", "start_line": 1, "end_line": 3, "note": "file not found"}]}],
        "dead_requirements": [req(5, "The system shall compute discounts.", dead_reason="file is not imported")],
        "dead_code": [{"kind": "file", "file": "app/legacy.py", "reason": "not imported"},
                      {"kind": "function", "file": "app/r.py", "name": "fmt", "line": 3, "end_line": 4,
                       "reason": "never referenced"}],
        "stats": {"repo": "d:/work/shop", "model": "gemini/gemini-3.8-flash", "confidence": {"confirmed": 0},
                  "surface_coverage": {"covered": 1, "total": 2, "uncovered": ["[env_var] DB_URL - app/db.py:3"]},
                  "function_coverage": {"cited": 2, "total": 3},
                  "type_coverage": {"cited": 1, "total": 1}, "asset_coverage": {"cited": 1, "files": 2},
                  "repo_files": {"git": True, "files": 9, "generated_skipped": [], "untracked_code_skipped": 0},
                  "reachability": {"analysed": True, "entry_points": ["app/main.py"]},
                  "cost": {"calls": 3, "input_tokens": 1000, "output_tokens": 100, "thinking_tokens": 50,
                           "cost_usd": 0.001, "provider_cost_usd": 0.0012, "credits": 10,
                           "by_task": {"module": {"calls": 3, "provider_cost_usd": 0.0012, "credits": 10}},
                           "by_model": {}, "unpriced_models": ["anthropic/x"]}},
    }
    assert render_pdf(result).startswith(b"%PDF")


def test_renders_older_and_empty_results():
    older = {"requirements": [req(1, "The API shall answer.")], "gaps": [], "rejected": [],
             "surfaces": [], "stats": {"repo": "e:\\backend\\svc", "model": "m"}}
    assert render_pdf(older).startswith(b"%PDF")
    assert render_pdf({}).startswith(b"%PDF")


def test_text_is_never_read_as_typst_markup():
    # Read as markup, #panic would stop the build and @nowhere would be an unknown reference.
    hostile = '#panic("markup was evaluated") @nowhere <x> $x^2$ *bold* ] [ \\ ` unbalanced'
    r = req(1, hostile, canonical_name="#panic(1)", section="#panic(2)",
            evidence=[{"file": "#panic(3)", "start_line": 1, "end_line": 1}])
    assert render_pdf({"requirements": [r], "stats": {"repo": "#panic(4)", "model": "$"}}).startswith(b"%PDF")
