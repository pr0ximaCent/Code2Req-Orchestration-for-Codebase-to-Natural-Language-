import os

from code2req.schema import Attribute, Evidence, FlatRequirement
from code2req.verify import FileCache, VerifyStats, is_test_path, value_in_code, verify_requirement

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "shop")


def req(evidence, confidence="inferred", attributes=None):
    return FlatRequirement(text="The system shall do X.", confidence=confidence,
                           evidence=evidence, attributes=attributes or [])


def run(r):
    stats = VerifyStats()
    out, ok = verify_requirement(r, FileCache(FIXTURE), stats)
    return out, ok, stats


def test_valid_evidence_passes_and_sets_source_file():
    out, ok, _ = run(req([Evidence(file="app/api.py", start_line=18, end_line=20,
                                   symbol="validate_quantity")]))
    assert ok
    assert out.source_file == "app/api.py"
    assert out.evidence[0].ok


def test_missing_file_wrong_line_and_wrong_symbol_fail():
    for ev, note in [
        (Evidence(file="app/nope.py", start_line=1, end_line=2), "file not found"),
        (Evidence(file="app/api.py", start_line=999, end_line=1000), "start_line outside file"),
        (Evidence(file="app/api.py", start_line=1, end_line=4, symbol="add_item"),
         "symbol 'add_item' not in cited lines"),
    ]:
        out, ok, stats = run(req([ev]))
        assert not ok
        assert out.evidence[0].note == note
        assert stats.rejected == 1


def test_one_good_evidence_is_enough_and_bad_ones_are_dropped():
    out, ok, stats = run(req([
        Evidence(file="app/nope.py", start_line=1, end_line=1),
        Evidence(file="./app/api.py", start_line=24, end_line=29, symbol="add_item"),
    ]))
    assert ok
    assert [e.file for e in out.evidence] == ["app/api.py"]
    assert stats.evidence_failed == 1


def test_confirmed_without_test_evidence_is_downgraded():
    out, ok, stats = run(req([Evidence(file="app/api.py", start_line=18, end_line=20)],
                             confidence="confirmed"))
    assert ok and out.confidence == "inferred"
    assert stats.downgraded_confirmed == 1

    out, ok, _ = run(req([Evidence(file="tests/test_api.py", start_line=7, end_line=10,
                                   symbol="validate_quantity")], confidence="confirmed"))
    assert ok and out.confidence == "confirmed"


def test_attribute_values_must_appear_in_code_with_unit_scaling():
    ev = [Evidence(file="app/api.py", start_line=6, end_line=7)]
    attrs = [
        Attribute(name="maxCartItems", operator="<=", value=50),
        Attribute(name="sessionTimeout", operator="<=", value=1.5, unit_literal="[s]"),  # 1500 ms
        Attribute(name="invented", operator="<=", value=77),
    ]
    out, ok, stats = run(req(ev, attributes=attrs))
    assert ok
    assert [a.name for a in out.attributes] == ["maxCartItems", "sessionTimeout"]
    assert stats.attributes_dropped == 1


def test_citation_inside_function_body_matches_enclosing_symbol():
    # Real-run case: lines 28-29 of add_item, whose def is on line 24.
    ev = [Evidence(file="app/api.py", start_line=28, end_line=29, symbol="add_item")]
    _, ok, _ = run(req(ev))
    assert ok  # nearest declaration above is `def add_item`
    files = FileCache(FIXTURE, symbols={"app/api.py": [(24, 29, "add_item")]})
    _, ok = verify_requirement(req(ev), files, VerifyStats())
    assert ok
    # a symbol that is not the enclosing declaration still fails
    wrong = [Evidence(file="app/api.py", start_line=28, end_line=29, symbol="validate_quantity")]
    out, ok, _ = run(req(wrong))
    assert not ok and "not in cited lines" in out.evidence[0].note


def test_attribute_value_resolved_through_named_constant():
    # Real-run case: "if len(cart) >= MAX_CART_ITEMS" cites the name, not 50.
    ev = [Evidence(file="app/api.py", start_line=26, end_line=27)]
    attrs = [Attribute(name="cartItems", operator="<=", value=50)]
    out, _, _ = run(req(ev, attributes=attrs))
    assert out.attributes == []
    files = FileCache(FIXTURE, constants={"MAX_CART_ITEMS": 50.0})
    out, _ = verify_requirement(req(ev, attributes=attrs), files, VerifyStats())
    assert [a.name for a in out.attributes] == ["cartItems"]


def test_citation_inside_long_constant_or_method_matches_declaration():
    from code2req.verify import declared_above
    go = [
        "package docreq",                                   # 1
        "",                                                 # 2
        "var extractionSystemPrompt = `You extract.",       # 3
        "EXTRACTION RULES:",                                # 4
        "- Extract ONLY lines with shall or must.",         # 5
        "`",                                                # 6
        "",                                                 # 7
        "var otherPrompt = `Other.",                        # 8
        "- something else",                                 # 9
        "`",                                                # 10
        "func (c *docxCursor) emitCurrentPara() {",         # 11
        "\ttext := label + text",                           # 12
        "}",                                                # 13
    ]
    assert declared_above(go, "extractionSystemPrompt", 5)
    assert not declared_above(go, "extractionSystemPrompt", 9)  # nearest declaration is otherPrompt
    assert declared_above(go, "emitCurrentPara", 12)
    assert not declared_above(go, "nothing", 12)

    files = FileCache(FIXTURE, symbols={"app/api.py": [(24, 29, "Cart.add_item")]})
    assert "add_item" in files.enclosing_symbols("app/api.py", 26, 27)


def test_value_in_code_and_test_paths():
    assert value_in_code(0.5, [500.0])
    assert not value_in_code(3.0, [500.0])
    assert is_test_path("tests/test_api.py")
    assert is_test_path("src/foo.spec.ts")
    assert not is_test_path("app/api.py")
