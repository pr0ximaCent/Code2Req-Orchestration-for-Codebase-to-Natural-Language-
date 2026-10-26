from types import SimpleNamespace

from code2req.agents.module_agent import render_code
from code2req.structure import (
    Module,
    _rename_root,
    assign_all_components,
    attach_declaration_files,
    split_large_modules,
)


def test_declaration_only_files_get_a_module(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.go").write_text("package pkg\nfunc A() {}\n")
    (tmp_path / "pkg" / "prompts.go").write_text('package pkg\nconst P = "only shall lines"\n')
    (tmp_path / "pkg" / "a_test.go").write_text("package pkg\n")
    (tmp_path / "pkg" / "gen.go").write_text("x" * 1000)
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "consts.py").write_text("LIMIT = 5\n")
    comps = {"a": node("pkg/a.go", 2, 10, name="A")}
    mods = attach_declaration_files([Module(name="pkg", path=["pkg"], component_ids=["a"])], comps,
                                    str(tmp_path), exclude=["gen.go"], max_chars=500)
    assert mods[0].extra_files == ["pkg/prompts.go"]  # same directory, fits
    assert [(m.name, m.extra_files) for m in mods[1:]] == [("consts", ["other/consts.py"])]


def test_render_code_sends_whole_files_or_declarations_for_split_files():
    lines = ["package p", "", "const Limit = 0.85", "", "func A() {", "  x()", "}",
             "", "func B() {", "  y()", "}"]
    a = node("p.go", 5, 0, name="A"); a.end_line = 7; a.id = "a"; a.source_code = "\n".join(lines[4:7])
    b = node("p.go", 9, 0, name="B"); b.end_line = 11; b.id = "b"; b.source_code = "\n".join(lines[8:11])
    comps = {"a": a, "b": b}
    read = lambda rel: lines if rel == "p.go" else None
    whole = render_code(["a", "b"], [], comps, read)
    assert "(whole file, lines 1-11)" in whole and " 3 | const Limit = 0.85" in whole
    part = render_code(["b"], [], comps, read)
    assert "top-level declarations" in part and "const Limit = 0.85" in part
    assert "func B()" in part and "func A()" not in part


def node(path, start, size, name="f"):
    return SimpleNamespace(relative_path=path, start_line=start, end_line=start + 1,
                           name=name, source_code="x" * size, id=name, component_type="function")


def test_split_packs_whole_files_and_labels_by_file():
    comps = {
        "a1": node("pkg/a.go", 1, 30), "a2": node("pkg/a.go", 9, 30),
        "b1": node("pkg/b.go", 1, 50),
        "c1": node("pkg/c.go", 1, 20),
        "d1": node("pkg/big.go", 1, 80), "d2": node("pkg/big.go", 50, 80),
    }
    mods = split_large_modules([Module(name="pkg", path=["pkg"], component_ids=list(comps))],
                               comps, max_chars=100)
    # a (60) fills a unit; big.go (160) alone exceeds the limit and is split by
    # component; b (50) and c (20) are packed together. No other file is cut.
    assert [(m.name, m.component_ids) for m in mods] == [
        ("a", ["a1", "a2"]),
        ("big (part 1)", ["d1"]),
        ("big (part 2)", ["d2"]),
        ("b, c", ["b1", "c1"]),
    ]
    assert mods[3].path == ["pkg", "b, c"]
    assert mods[3].section == "pkg > b, c"


def test_small_module_is_kept_whole():
    comps = {"a": node("x/a.py", 1, 10), "b": node("x/b.py", 1, 10)}
    mods = split_large_modules([Module(name="x", path=["x"], component_ids=["b", "a"])], comps)
    assert [(m.name, m.component_ids) for m in mods] == [("x", ["a", "b"])]


def test_unassigned_components_follow_their_file_then_directory():
    comps = {"a": node("svc/a.py", 1, 5), "a_helper": node("svc/a.py", 20, 5),
             "z": node("other/z.py", 1, 5), "r": node("main.py", 1, 5)}
    mods = assign_all_components([Module(name="svc", path=["svc"], component_ids=["a"])], comps)
    _rename_root(mods, "repo")
    assert [(m.name, sorted(m.component_ids)) for m in mods] == \
        [("svc", ["a", "a_helper"]), ("repo", ["r"]), ("other", ["z"])]
