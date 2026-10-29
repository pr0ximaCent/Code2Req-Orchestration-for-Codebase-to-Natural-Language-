import json
import os

from code2req.dependencies import enrich_dependency_graph
from code2req.structure import build_structure


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build(tmp_path):
    repo = tmp_path / "repo"
    _write(repo, "app/__init__.py", "")
    _write(repo, "app/config.py", "MAX_ITEMS = 50\n\ndef limit():\n    return MAX_ITEMS\n")
    _write(repo, "app/api.py", (
        "import os\n"
        "import json as js\n"
        "from app.config import MAX_ITEMS, limit\n"
        "from .config import limit as lim2\n"
        "from requests import get\n"
        "\n"
        "TIMEOUT = 5\n"
        "\n"
        "def add(items, n):\n"
        "    TIMEOUT = 1  # local, shadows the module variable\n"
        "    if len(items) >= MAX_ITEMS:\n"
        "        raise ValueError(os.getenv('X'))\n"
        "    return js.dumps(limit())\n"
        "\n"
        "def fetch():\n"
        "    from collections import OrderedDict\n"
        "    return get('u', timeout=TIMEOUT), OrderedDict(), helper()\n"
        "\n"
        "def helper():\n"
        "    return lim2()\n"
    ))
    work = tmp_path / "work"
    st = build_structure(str(repo), str(work), None, "m")
    return st, work


def test_depends_on_includes_imports_and_module_variables(tmp_path):
    st, work = _build(tmp_path)
    deps = {c.name: c.depends_on for c in st.components.values()}
    cfg = "app/config.py"
    limit_id = next(i for i, c in st.components.items()
                    if c.name == "limit" and c.relative_path.replace("\\", "/") == cfg)

    assert {"import:os", "import:json", limit_id, f"global:{cfg}::MAX_ITEMS"} <= deps["add"]
    assert "global:app/api.py::TIMEOUT" not in deps["add"]  # shadowed by a local
    assert not any("len" in d for d in deps["add"])  # builtins are left out

    assert {"import:requests.get", "import:collections.OrderedDict",
            "global:app/api.py::TIMEOUT"} <= deps["fetch"]
    assert limit_id in deps["helper"]  # `from .config import limit as lim2`
    assert f"global:{cfg}::MAX_ITEMS" in deps["limit"]


def test_graph_json_is_patched_with_the_new_entries(tmp_path):
    st, work = _build(tmp_path)
    path = work / "temp" / "dependency_graphs" / "repo_dependency_graph.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    add_id = next(i for i, c in st.components.items() if c.name == "add")
    assert "import:os" in data[add_id]["depends_on"]
    assert data[add_id]["depends_on"] == sorted(data[add_id]["depends_on"])
    assert set(data) == set(st.components)
    assert os.path.exists(path)
    assert enrich_dependency_graph(st.components, str(tmp_path / "repo"), str(path.parent)) == 0
