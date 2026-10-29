from code2req import assets
from code2req.agents.asset_agent import build_asset_prompt
from code2req.reachability import analyze

REPO = {
    "app/__init__.py": "",
    "app/main.py": (
        "from fastapi import FastAPI\n"
        "from app import chat, mail\n"
        "app = FastAPI()\n"
    ),
    "app/paths.py": (
        "from pathlib import Path\n"
        "BASE = Path(__file__).parent.parent\n"
        "PROMPTS_DIR = BASE / 'templates' / 'prompts'\n"
        "PLAN_PROMPT = PROMPTS_DIR / 'plan.txt'\n"
    ),
    "app/chat.py": (
        "from app.paths import PLAN_PROMPT, BASE\n"
        "kb = KnowledgeBase(root=BASE / 'app' / 'knowledge')\n"
        "def plan():\n"
        "    return PLAN_PROMPT.read_text()\n"
        "def mode(x):\n"
        "    return x == 'examples'\n"
    ),
    "app/mail.py": "TEMPLATE = 'welcome.html'\ndef send():\n    return TEMPLATE\n",
    "app/legacy.py": "def old():\n    return open('templates/prompts/legacy.txt').read()\n",
    "templates/prompts/plan.txt": "# Plan\nYou MUST answer with numbered steps.\n",
    "templates/prompts/legacy.txt": "Old prompt.\n",
    "templates/prompts/orphan.txt": "Nobody loads me.\n",
    "templates/mail/welcome.html": "<p>Hello {{name}}</p>\n",
    "app/knowledge/INDEX.md": "# Index\n## Parts\nUse part defs.\n## Rules\nNEVER nest packages.\n",
    "app/knowledge/examples/drone.sysml": "package Drone {\n" + "    part def Rotor;\n" * 200 + "}\n",
    "docs/guide.md": "# Guide\n",
    "README.md": "# Readme\n",
    "Dockerfile": "FROM python:3.12\nCMD uvicorn app.main:app\n",
    ".github/workflows/ci.yml": "on: push\njobs: {}\n",
    "deploy/service.yaml": "kind: Service\n",
    "poetry.lock": "x\n",
}


def write(root, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def test_classify_roles():
    assert assets.classify(".github/workflows/ci.yml") == "cicd"
    assert assets.classify("Dockerfile") == "cicd"
    assert assets.classify("k8s/app.yaml") == "cicd"
    assert assets.classify("app/skills/v2/x/SKILL.md") == "skill"
    assert assets.classify("app/knowledge/09_examples/a.sysml") == "reference"
    assert assets.classify("templates/prompts/a.txt") == "prompt"
    assert assets.classify("templates/chatbot/system.txt") == "prompt"
    assert assets.classify("templates/email/verify.html") == "template"
    assert assets.classify("app/data/limits.yaml") == "config"


def test_discover_links_loaded_files_and_marks_the_rest(tmp_path):
    write(tmp_path, REPO)
    found = assets.discover(str(tmp_path), analyze(str(tmp_path)))
    roles = {a.file: a.role for a in found.assets}
    assert roles == {
        ".github/workflows/ci.yml": "cicd",
        "Dockerfile": "cicd",
        "deploy/service.yaml": "cicd",
        "templates/prompts/plan.txt": "prompt",        # through PLAN_PROMPT -> PROMPTS_DIR
        "templates/mail/welcome.html": "template",     # named in mail.py
        "app/knowledge/INDEX.md": "reference",         # the folder is loaded whole
        "app/knowledge/examples/drone.sysml": "reference",
    }
    plan = next(a for a in found.assets if a.file == "templates/prompts/plan.txt")
    assert plan.loaded_by[0][:2] == ("app/chat.py", 4)
    # legacy.txt is only loaded by dead code; orphan.txt by nothing, next to loaded prompts.
    assert {(d.file, d.reason.split(" (")[0]) for d in found.dead} == {
        ("templates/prompts/legacy.txt", "loaded only by dead code"),
        ("templates/prompts/orphan.txt", "no code loads this file, though files next to it are loaded"),
    }
    # Docs, READMEs and lock files are never included.
    assert "docs/guide.md" not in roles and "README.md" not in roles and "poetry.lock" not in roles


def test_only_tracked_files_and_no_test_loaders(tmp_path):
    import subprocess
    files = dict(REPO)
    files["app/data_test.go"] = 'package app\nvar f = "templates/prompts/orphan.txt"\n'
    files["debug-dumps/run1/out.json"] = "{}\n"
    files["app/dump.py"] = "OUT = 'debug-dumps'\ndef save():\n    return open(OUT + '/run1/out.json')\n"
    files["deployment-files/blue/service.yaml"] = "kind: Service\n"
    write(tmp_path, files)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    tracked = [p for p in files if not p.startswith("debug-dumps/")]
    subprocess.run(["git", "add", *tracked], cwd=tmp_path, check=True)
    found = assets.discover(str(tmp_path), analyze(str(tmp_path)))
    roles = {a.file: a.role for a in found.assets}
    assert not any(p.startswith("debug-dumps/") for p in roles)  # untracked
    assert roles["deployment-files/blue/service.yaml"] == "cicd"
    # A test naming orphan.txt does not make it loaded.
    assert "templates/prompts/orphan.txt" in {d.file for d in found.dead}


def test_folder_name_in_a_non_path_string_does_not_link(tmp_path):
    files = dict(REPO)
    files["app/chat.py"] = "def mode(x):\n    return x == 'knowledge'\n"
    write(tmp_path, files)
    found = assets.discover(str(tmp_path), analyze(str(tmp_path)))
    assert not any(a.file.startswith("app/knowledge/") for a in found.assets)


def test_modules_group_by_folder_and_outline_long_reference_files(tmp_path):
    write(tmp_path, REPO)
    found = assets.discover(str(tmp_path), analyze(str(tmp_path)))
    mods = assets.build_modules(found.assets, max_chars=40_000)
    # Packed per top-level folder pair, so mail templates and prompts are separate units.
    assert sorted(m.section for m in mods) == [
        "assets > app/knowledge", "assets > ci-cd", "assets > templates/mail", "assets > templates/prompts"]
    kb = next(m for m in mods if m.section == "assets > app/knowledge")
    prompt = build_asset_prompt(kb)
    assert "### app/knowledge/examples/drone.sysml (OUTLINE, whole file, lines 1-202)" in prompt
    assert "LOADED BY:\n- app/chat.py:2:" in prompt
    assert "202 | }" in prompt  # the outline keeps real line numbers
    assert prompt.count("part def Rotor") < 200


def test_markdown_sections_become_surfaces(tmp_path):
    write(tmp_path, REPO)
    found = assets.discover(str(tmp_path), analyze(str(tmp_path)))
    surf = [(s.kind, s.name) for s in assets.asset_surfaces(found.assets) if s.file == "templates/prompts/plan.txt"]
    assert surf == [("asset", "templates/prompts/plan.txt")]
    index = [(s.kind, s.name, s.line, s.end_line) for s in assets.asset_surfaces(found.assets)
             if s.file == "app/knowledge/INDEX.md"]
    # Reference files get one surface each, no section surfaces.
    assert index == [("asset", "app/knowledge/INDEX.md", 1, 5)]
