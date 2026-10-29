from code2req.reachability import analyze


def write(root, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


APP = {
    "app/__init__.py": "",
    "app/main.py": (
        "import importlib\n"
        "from fastapi import FastAPI\n"
        "from app.services import used\n"
        "from .routes import router\n"
        "app = FastAPI()\n"
        "plugin = importlib.import_module('app.plugins.loaded')\n"
        "plugin.run()\n"
    ),
    "app/routes.py": (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/x')\n"
        "def handler():\n"
        "    used.Store().save()\n"
        "    return used.helper()\n"
    ),
    "app/services/__init__.py": "",
    "app/services/used.py": (
        "from starlette.middleware.base import BaseHTTPMiddleware\n"
        "def helper():\n"
        "    return 1\n"
        "def never_called():\n"
        "    return 2\n"
        "def called_by_name():\n"
        "    return 3\n"
        "HANDLERS = {'x': 'called_by_name'}\n"
        "class Store:\n"
        "    def save(self):\n"
        "        return self.unused_method()\n"
        "    def unused_method(self):\n"
        "        return 4\n"
        "    def orphan(self):\n"
        "        return 5\n"
        "class Middleware(BaseHTTPMiddleware):\n"
        "    def dispatch(self, request, call_next):\n"
        "        return call_next(request)\n"
    ),
    "app/plugins/__init__.py": "",
    "app/plugins/loaded.py": "def run():\n    return 1\n",
    "app/legacy.py": "def old():\n    return 0\n",
    "app/legacy_pkg/__init__.py": "",
    "scripts/migrate.py": "if __name__ == '__main__':\n    print('migrate')\n",
    "tests/test_legacy.py": "from app.legacy import old\n",
}


def test_dead_files_are_not_imported_from_any_entry_point(tmp_path):
    write(tmp_path, APP)
    r = analyze(str(tmp_path))
    assert r.analysed
    assert r.entries == ["app/main.py", "scripts/migrate.py"]
    # A test importing legacy.py does not make it live; the string import keeps plugins live;
    # an empty __init__.py is not reported.
    assert r.dead_files == ["app/legacy.py"]
    assert "app/plugins/loaded.py" in r.live_files and "app/routes.py" in r.live_files
    assert r.is_dead_range("app/legacy.py", 1, 2)


def test_dead_functions_are_never_named_by_live_code(tmp_path):
    write(tmp_path, APP)
    r = analyze(str(tmp_path))
    # Not dead: helper (called), called_by_name (named in a string), unused_method (called via self),
    # handler (decorated), dispatch (framework base class), anything in a dead file.
    assert [(f.file, f.name) for f in r.dead_functions] == [
        ("app/services/used.py", "never_called"), ("app/services/used.py", "orphan")]
    assert r.is_dead_range("app/services/used.py", 4, 5)
    assert not r.is_dead_range("app/services/used.py", 2, 3)
    assert {d.kind for d in r.report()} == {"file", "function"}


def test_entry_points_from_launch_files_and_extra_globs(tmp_path):
    write(tmp_path, {
        "svc/__init__.py": "",
        "svc/server.py": "def serve():\n    return 1\n",
        "svc/worker.py": "def work():\n    return 1\n",
        "svc/tool.py": "def go():\n    return 1\n",
        "Dockerfile": 'CMD ["uvicorn", "svc.server:app"]\n',
        "pyproject.toml": '[project.scripts]\nworker = "svc.worker:work"\n',
    })
    r = analyze(str(tmp_path), extra_entries=["svc/tool.py"])
    assert r.entries == ["svc/__init__.py", "svc/server.py", "svc/tool.py", "svc/worker.py"]
    assert r.dead_files == []


def test_no_entry_point_means_everything_is_live(tmp_path):
    write(tmp_path, {"lib/a.py": "def f():\n    return 1\n"})
    r = analyze(str(tmp_path))
    assert not r.analysed and r.dead_files == [] and r.dead_functions == []
    assert not r.is_dead_range("lib/a.py", 1, 2)
