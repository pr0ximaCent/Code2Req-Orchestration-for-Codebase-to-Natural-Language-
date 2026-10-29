"""Python relationship graph: nodes, links and line ownership, on small repos with real CodeWiki parsing."""

import json

import pytest

from code2req.structure import build_structure

CONFIG = '''"""Limits."""
# carts above this size are rejected
MAX_ITEMS = 50
NAME, OTHER = "a", "b"


def limit():
    return MAX_ITEMS
'''

API = '''#!/usr/bin/env python
# TODO: move to a service

import os
import json as js
from app.config import MAX_ITEMS, limit
from .config import limit as lim2
from requests import get
import app.config

TIMEOUT = 5  # seconds
RETRIES = TIMEOUT * 2

# helper used below
def helper():
    return lim2()


class Base:
    LEVEL = 1

    def __init__(self):
        self.size = 0

    def grow(self):
        self.size += 1


class Cart(Base):
    KIND = "cart"

    def __init__(self, items):
        super().__init__()
        self.items = items
        self.total = 0

    def add(self, n):
        TIMEOUT = 1  # local, shadows the module variable
        if len(self.items) >= MAX_ITEMS:
            raise ValueError(os.getenv("X"))
        self.total = self.total + n + self.LEVEL + Cart.KIND
        self.grow()
        return js.dumps(limit()), app.config.MAX_ITEMS

    def missing(self):
        return self.nothing_here


def fetch():
    from collections import OrderedDict

    def inner():
        return RETRIES

    return get("u", timeout=TIMEOUT), OrderedDict(), helper(), inner()


count = 0


def bump():
    global count
    count += 1


for i in range(3):
    fetch()

if __name__ == "__main__":
    bump()
# trailing note
'''


@pytest.fixture()
def built(tmp_path):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text("")
    (repo / "app" / "config.py").write_text(CONFIG, encoding="utf-8")
    (repo / "app" / "api.py").write_text(API, encoding="utf-8")
    work = tmp_path / "work"
    st = build_structure(str(repo), str(work), None, "m")
    graph = json.loads((work / "temp" / "dependency_graphs" / "repo_dependency_graph.json").read_text("utf-8"))
    report = json.loads((work / "temp" / "dependency_graphs" / "repo_line_ownership.json").read_text("utf-8"))
    by_name = {}
    for e in graph.values():
        by_name.setdefault((e["relative_path"].replace("\\", "/"), e["name"]), e)
    return st, graph, report, by_name


def entry(by_name, rel, name):
    return by_name[(rel, name)]


def test_module_level_nodes_exist(built):
    _st, _graph, _report, by = built
    api = "app/api.py"
    assert entry(by, api, "TIMEOUT")["component_type"] == "constant"
    assert entry(by, api, "TIMEOUT")["value"] == "5"
    assert entry(by, api, "count")["component_type"] == "variable"
    assert entry(by, "app/config.py", "NAME, OTHER")["names"] == ["NAME", "OTHER"]
    assert entry(by, "app/config.py", "docstring")["component_type"] == "docstring"
    assert entry(by, api, 'if __name__ == "__main__"')["component_type"] == "main_script"
    types = {e["component_type"] for e in by.values()}
    assert {"imports", "script_block", "module", "comment_block"} <= types


def test_function_links_point_at_real_node_ids(built):
    _st, graph, _r, by = built
    add = entry(by, "app/api.py", "Cart.add")
    deps = set(add["depends_on"])
    max_items = entry(by, "app/config.py", "MAX_ITEMS")["id"]
    limit_id = entry(by, "app/config.py", "limit")["id"]
    assert {max_items, limit_id, "import:os", "import:json"} <= deps
    assert entry(by, "app/api.py", "TIMEOUT")["id"] not in deps  # shadowed by a local
    assert {"builtin:len", "builtin:ValueError"} <= deps
    assert max_items in add["reads"]
    assert all(d in graph or d.startswith(("import:", "builtin:")) for d in deps)  # no dangling ids


def test_module_attribute_access_resolves_through_the_module(built):
    _st, _g, _r, by = built
    add = entry(by, "app/api.py", "Cart.add")
    assert entry(by, "app/config.py", "MAX_ITEMS")["id"] in add["depends_on"]


def test_self_attributes_class_attributes_and_inheritance(built):
    _st, _g, _r, by = built
    api = "app/api.py"
    total = entry(by, api, "Cart.total")
    items = entry(by, api, "Cart.items")
    level = entry(by, api, "Base.LEVEL")
    size = entry(by, api, "Base.size")
    kind = entry(by, api, "Cart.KIND")
    assert total["component_type"] == "instance_attribute" and total["parent"] == entry(by, api, "Cart")["id"]
    assert level["component_type"] == "class_attribute" and level["value"] == "1"
    assert kind["value"] == "'cart'"

    add = entry(by, api, "Cart.add")
    assert {total["id"], items["id"], level["id"], kind["id"]} <= set(add["depends_on"])
    assert {total["id"], items["id"], level["id"], kind["id"]} == set(add["reads"]) - {
        entry(by, "app/config.py", "MAX_ITEMS")["id"]}
    assert total["id"] in add["writes"]
    assert entry(by, api, "Base.grow")["id"] in add["depends_on"]  # self.grow() found in the base class

    grow = entry(by, api, "Base.grow")
    assert size["id"] in grow["reads"] and size["id"] in grow["writes"]  # self.size += 1
    assert entry(by, api, "Cart.missing")["unresolved_attributes"] == ["self.nothing_here"]
    init_size_writes = size["writes_at"]
    assert len(init_size_writes) == 2  # __init__ and grow


def test_module_level_blocks_and_globals_are_linked(built):
    _st, _g, _r, by = built
    api = "app/api.py"
    retries = entry(by, api, "RETRIES")
    assert entry(by, api, "TIMEOUT")["id"] in retries["depends_on"]
    bump = entry(by, api, "bump")
    assert entry(by, api, "count")["id"] in bump["writes"]
    main = entry(by, api, 'if __name__ == "__main__"')
    assert bump["id"] in main["depends_on"]
    script = next(e for e in by.values() if e["component_type"] == "script_block" and e["relative_path"].endswith("api.py"))
    assert entry(by, api, "fetch")["id"] in script["depends_on"]
    fetch = entry(by, api, "fetch")
    assert {"import:requests.get", "import:collections.OrderedDict"} <= set(fetch["depends_on"])
    assert entry(by, api, "RETRIES")["id"] in fetch["depends_on"]  # read by the nested function
    assert entry(by, api, "fetch.inner")["synthetic"] is True  # nested def CodeWiki does not list


def test_imports_node_and_module_node_link_files(built):
    _st, _g, _r, by = built
    imports = next(e for e in by.values() if e["component_type"] == "imports" and e["relative_path"].endswith("api.py"))
    cfg_module = next(e for e in by.values() if e["component_type"] == "module" and e["relative_path"].endswith("config.py"))
    assert cfg_module["id"] in imports["depends_on"]
    assert entry(by, "app/config.py", "limit")["id"] in imports["depends_on"]
    assert "import:requests.get" in imports["depends_on"]
    api_module = next(e for e in by.values() if e["component_type"] == "module" and e["relative_path"].endswith("api.py"))
    assert cfg_module["id"] in api_module["depends_on"]
    assert imports["id"] in api_module["children"]


def test_comments_attach_to_nodes_or_become_blocks(built):
    _st, _g, _r, by = built
    api = "app/api.py"
    max_items = entry(by, "app/config.py", "MAX_ITEMS")
    assert [c["text"] for c in max_items["leading_comments"]] == ["# carts above this size are rejected"]
    assert [c["text"] for c in entry(by, api, "helper")["leading_comments"]] == ["# helper used below"]
    header = entry(by, api, "comments L1-2")
    assert header["component_type"] == "comment_block" and header["tag"] == "TODO"
    assert "# TODO: move to a service" in header["source_code"]
    assert header["describes"] == next(
        e["id"] for e in by.values() if e["component_type"] == "imports" and e["relative_path"].endswith("api.py"))
    tail = next(e for e in by.values() if e["component_type"] == "comment_block" and "trailing note" in e["source_code"])
    assert tail["describes"] is None
    assert "seconds" in entry(by, api, "TIMEOUT")["source_code"]  # trailing comment stays in the node


def test_line_ownership_is_complete_and_beats_codewiki_alone(built):
    _st, _g, report, _by = built
    s = report["summary"]
    assert s["unparsed_files"] == []
    assert s["ratio_graph"] == 1.0
    assert s["ratio_codewiki"] < s["ratio_graph"]
    assert s["comment_lines_graph"] == s["comment_lines"] > s["comment_lines_codewiki"]
    assert all(f["unowned"] == [] for f in report["files"].values())


def test_existing_fields_and_call_edges_are_kept(built):
    st, graph, _r, _by = built
    for cid, comp in st.components.items():
        e = graph[cid]
        assert e["source_code"] == comp.source_code and e["start_line"] == comp.start_line
        # JSON and in-memory agree; builtins are in the JSON only
        assert {d for d in e["depends_on"] if not d.startswith("builtin:")} == set(comp.depends_on)
    helper_id = next(i for i, c in st.components.items() if c.name == "helper")
    assert any(d.endswith("config.py::limit") for d in graph[helper_id]["depends_on"])


def test_unparsable_file_is_reported_not_fatal(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ok.py").write_text("X = 1\n\ndef f():\n    return X\n")
    (repo / "bad.py").write_text("def broken(:\n")
    work = tmp_path / "work"
    build_structure(str(repo), str(work), None, "m")
    report = json.loads((work / "temp" / "dependency_graphs" / "repo_line_ownership.json").read_text("utf-8"))
    assert report["summary"]["unparsed_files"] == [{"file": "bad.py", "reason": "Python syntax error"}]
    assert report["summary"]["ratio_graph"] == 1.0


MODELS = '''from typing import Optional


class Engine:
    def run(self):
        return 1

    def stop(self):
        return 0


class Turbo(Engine):
    def run(self):
        return super().run() + 1


class Boom(Exception):
    pass


def deco(fn):
    return fn


engine = Engine()


class Car:
    def __init__(self):
        self.engine = Turbo()

    @deco
    @property
    def power(self) -> Optional[Engine]:
        return self.engine.run()

    def drive(self, e: Engine):
        local = Turbo()
        local.stop()
        engine.run()
        return sorted(str(x) for x in range(3))
'''


def build_models(tmp_path, **kw):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "models.py").write_text(MODELS, encoding="utf-8")
    work = tmp_path / "work"
    build_structure(str(repo), str(work), None, "m", **kw)
    graph = json.loads((work / "temp" / "dependency_graphs" / "repo_dependency_graph.json").read_text("utf-8"))
    return {e["name"]: e for e in graph.values()}


def test_inheritance_decorators_annotations_and_overrides(tmp_path):
    by = build_models(tmp_path)
    eid = lambda n: by[n]["id"]  # noqa: E731
    assert by["Turbo"]["inherits"] == [eid("Engine")]
    assert by["Boom"]["inherits"] == ["builtin:Exception"]
    assert by["Car.power"]["decorators"] == sorted([eid("deco"), "builtin:property"])
    assert eid("Engine") in by["Car.power"]["annotations"] and "import:typing.Optional" in by["Car.power"]["annotations"]
    assert by["Car.drive"]["annotations"] == [eid("Engine")]
    assert by["Turbo.run"]["overrides"] == [eid("Engine.run")]
    assert by["Engine.run"]["overrides"] == []


def test_calls_through_constructed_instances_are_resolved(tmp_path):
    by = build_models(tmp_path)
    eid = lambda n: by[n]["id"]  # noqa: E731
    drive = set(by["Car.drive"]["depends_on"])
    assert eid("Engine.stop") in drive  # local = Turbo(); local.stop() found on the base class
    assert eid("Engine.run") in drive  # engine = Engine() at module level; engine.run()
    assert {"builtin:sorted", "builtin:str", "builtin:range"} <= drive
    power = set(by["Car.power"]["depends_on"])
    assert eid("Turbo.run") in power  # self.engine = Turbo() in __init__; self.engine.run()
    assert by["Car.engine"]["id"] in by["Car.power"]["reads"]


def test_no_builtins_flag_drops_builtin_entries(tmp_path):
    by = build_models(tmp_path, builtins=False)
    everything = {d for e in by.values() for d in e["depends_on"]}
    assert not any(d.startswith("builtin:") for d in everything)
    assert by["Boom"]["inherits"] == []
