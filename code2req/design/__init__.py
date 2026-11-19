"""The system design of a codebase, recovered from its code, written as a document SysModeler models.

code2req's requirements say what the code must do; the design says what the code is made of and how
it behaves: its parts and subsystems, the connections between them and what each carries, the
external systems, the routes, settings and data, the state machines, activities and interactions.
`write_design` reads a finished run (requirements.json) and the source, and writes design.json,
system_design.md/.txt/.pdf and the model itself (system_model/ and a .kpar SysModeler imports) into the
run's folder.

Two readers: reader.py (the default), for any language the outline parses (Go, Python, TypeScript,
JavaScript, Java, C#): the model reads, code checks every answer against the source; and golang.py,
hand-written rules for Go services wired in one place (`reader="go"`), kept as the reference the first
is tested against."""
from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter

log = logging.getLogger(__name__)
GENERIC_NAMES = {"backend", "server", "service", "api", "app", "src", "code", "repo", "main", "core"}


def supported(repo: str) -> str | None:
    """The main language of the repo's source, when the design reader handles it; else None."""
    from .outline import LANG_OF
    from .reader import source_files
    langs = Counter(LANG_OF[os.path.splitext(f)[1].lower()] for f in source_files(repo))
    return langs.most_common(1)[0][0] if langs else None


def title_for(repo: str, target: str = "") -> str:
    """A name for the system: the repo's (or its parent's, when the folder is called backend/server...)."""
    path = (target or repo).rstrip("/\\")
    if re.match(r"(https?://|git@)", path):
        name = re.sub(r"\.git$", "", path.split("/")[-1].split(":")[-1])
    else:
        parts = [p for p in re.split(r"[/\\]", os.path.abspath(path)) if p]
        name = parts[-1]
        if name.lower() in GENERIC_NAMES and len(parts) > 1:
            name = parts[-2]
    return " ".join(w.capitalize() for w in re.split(r"[-_ .]+", name) if w) or "System"


def anchor(design: dict, report: dict) -> tuple[list[dict], dict, dict, list[str]]:
    """Each requirement's subject (the component whose code its evidence cites most) and the
    operations and transitions that implement it; requirements citing no component belong to the system."""
    kept, subject, impl = [], {}, {}
    for r in report.get("requirements", []):
        ids, comps, cited = [], Counter(), set()
        for e in r.get("evidence", []):
            cited.add(e["file"])
            for f, s, t, eid, comp in design["spans"]:
                if f == e["file"] and s <= e["end_line"] and e["start_line"] <= t:
                    if eid:
                        ids.append(eid)
                    comps[comp] += 1
        if comps or cited:
            kept.append(r)
            subject[r["id"]] = comps.most_common(1)[0][0] if comps else design["title"]
            impl[r["id"]] = ", ".join(dict.fromkeys(ids))
    left_out = [r["id"] for r in report.get("requirements", []) if r["id"] not in subject]
    return kept, subject, impl, left_out


def fix_route_text(text: str, design: dict, subject: str, impl: str) -> tuple[str, list[tuple[str, str]]]:
    """A route a requirement names must be one the code registers: a wrong one is replaced by the route
    of its subject with that ending, and an HTTP method named without a path by the method of the one
    route its implementing handler serves."""
    known = {(r["method"], r["path"]) for r in design["routes"]}
    mine = [r for r in design["routes"] if r["handler"].split(".")[0] and any(
        c["name"] == subject and r["handler"].split(".")[0] in [p["name"] for p in c["parts"]] for c in design["components"])]
    fixes = []

    def pick(method, path):
        """The one route a wrong (method, path) means: same method and the same ending first, then looser."""
        path = path.rstrip("/") or "/"
        tiers = [[r for r in mine if r["method"] == method and r["path"].endswith(path)],
                 [r for r in mine if r["method"] == method and path.split("/")[-1] in r["path"].split("/")],
                 [r for r in mine if r["path"].endswith(path)]]
        for tier in tiers:
            if len({(r["method"], r["path"]) for r in tier}) == 1:
                return tier[0]
        return None

    def repl_path(m):
        method, path = m.group(1), m.group(3).rstrip(".,;:")
        if (method, path.rstrip("/") or "/") in known:
            return m.group(0)
        r = pick(method, path)
        if r is None:
            return m.group(0)
        fixes.append((f"{method} {path}", f"{r['method']} {r['path']}"))
        return f"{r['method']}{m.group(2)}{r['path']}" + m.group(3)[len(path):]
    # "POST /documents" and "a POST request at /documents"
    text = re.sub(r"\b(GET|POST|PUT|PATCH|DELETE)(\s+(?:requests?\s+(?:at|to|on|for)\s+)?)(/[\w/{}\-:.]*)", repl_path, text)
    ops = set(re.findall(r"OP_\d+", impl))
    methods = {r["method"] for r in design["routes"] if r["op"] in ops}
    if len(methods) == 1:
        right = next(iter(methods))

        def repl_method(m):
            if m.group(1) != right:
                fixes.append((m.group(1), right))
                return right
            return m.group(0)
        text = re.sub(r"\b(GET|POST|PUT|PATCH|DELETE)\b(?!\s+/)", repl_method, text)
    return text, fixes


# short words in setting names and the words requirements write for them
SYNONYMS = {"db": "database", "addr": "address", "coll": "collection", "max": "maximum", "min": "minimum",
            "num": "number", "cfg": "configuration", "conn": "connection", "ttl": "time to live", "dir": "directory"}
CONFIGURED = re.compile(r"\bconfigur\w*|\bsettings?\b|\benvironment\b", re.I)


def settings_used(text: str, settings: list[dict]) -> list[dict]:
    """The settings a requirement relies on: named by environment variable or by its value
    ('ps_raw_chunks'), or by the words of the setting's name ('read timeout' for readTimeout, 'database
    name' for dbName). A one-word name ('uri', 'host') counts only where another word of the sentence
    says the value is configured."""
    low, out = text.lower(), []
    for s in settings:
        value = str(s.get("value", "")).strip('"`')
        if (s.get("env") and re.search(rf"\b{re.escape(s['env'])}\b", text)) \
                or (len(value) >= 4 and re.search(rf"(?<![\w/]){re.escape(value)}(?![\w/])", text)):
            out.append(s)
            continue
        ws = [w.lower() for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", s["name"]) if w.lower() != "value"]
        if not ws:
            continue
        alts = ["(?:" + "|".join(re.escape(a) for a in (w, SYNONYMS.get(w)) if a) + ")s?" for w in ws]
        named = re.compile(r"\b" + r"[\s-]+".join(alts) + r"\b(?![\s-]+variables?)")  # not "environment variables"
        if named.search(low) and (len(ws) > 1 or CONFIGURED.search(named.sub(" ", low))):
            out.append(s)
    return out


def load_libraries() -> None:
    """Import the model clients before the design is read on its own thread: two threads importing openai for the
    first time at once deadlock on Python's import lock (seen with the requirements pass, which imports it lazily)."""
    import importlib
    for name in ("openai", "openai.types", "openai.types.chat", "litellm", "codewiki.src.be.llm_services"):
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 - a module this install lacks is simply not used
            pass


def read_design(repo: str, llm, title: str | None = None, target: str = "", reader: str = "llm", report: dict | None = None,
                progress=None) -> dict | None:
    """The design read from the source alone (the slow part: model calls), so it can run while the requirements are
    written. report: the run's report when it exists, else the surfaces are found here. None when the repo has no
    source in a language the reader handles."""
    if supported(repo) is None:
        log.info("design: no source in a language the design reader handles (Go, Python, TypeScript, JavaScript, Java, C#)")
        return None
    title = title or title_for(repo, target)
    from .reader import Reader, source_files
    files = source_files(repo)
    if report is None:
        from ..surfaces import detect_surfaces
        report = {"surfaces": [x.model_dump() for x in detect_surfaces(repo, files)]}
    if reader == "go" and os.path.isfile(os.path.join(repo, "go.mod")):
        from .golang import Design
        return Design(repo, report, title).build()
    return Reader(repo, report, title, llm, files, progress=progress).build()


def write_design(run_dir: str, repo: str, llm, title: str | None = None, target: str = "", reader: str = "llm",
                 progress=None, design: dict | None = None) -> dict | None:
    """Attach the run's requirements to the design and write the documents and the model. design: one read_design
    already read (while the requirements were written); else it is read here. Returns a summary, or None when the
    repo has no source in a language the reader handles."""
    with open(os.path.join(run_dir, "requirements.json"), encoding="utf-8") as f:
        report = json.load(f)
    title = title or title_for(repo, target)
    if design is None:
        design = read_design(repo, llm, title, target, reader, report, progress)
    if design is None:
        return None
    design["source"] = target or repo
    kept, subject, impl, left_out = anchor(design, report)
    atomic, seen = [], {}
    for r in kept:  # each requirement as the run wrote it; names unique
        name = r.get("name") or r["id"]
        seen[name] = seen.get(name, 0) + 1
        atomic.append({"id": r["id"], "source": r["id"], "name": name if seen[name] == 1 else f"{name}_{seen[name]}",
                       "text": r["text"], "kind": r.get("type", "functional"), "attributes": []})
    fixes = []
    for a in atomic:
        src = a.get("source", a["id"])
        a["subject"] = subject.get(src, design["title"])
        a["implemented_by"] = impl.get(src, "")
        a["text"], f = fix_route_text(a["text"], design, a["subject"], a["implemented_by"])
        fixes += f
        # the model gives an attribute its value from the requirement that states it: name the defaults here
        a["uses"] = [s["id"] for s in settings_used(a["text"], design["settings"])]
    if fixes:
        design["decisions"].append("Routes named in requirements were checked against the routes the code registers: "
                                   + "; ".join(f"{a} → {b}" for a, b in dict.fromkeys(fixes)) + ".")
    design["requirements"] = atomic
    design["left_out_requirements"] = left_out
    from .render import write
    stats = write(design, run_dir)
    # the model itself, as SysML files and as a project archive SysModeler imports
    from .sysml import write_kpar, write_sysml
    model_dir = os.path.join(run_dir, "system_model")
    if os.path.isdir(model_dir):  # a file of an earlier design must not stay behind
        import shutil
        shutil.rmtree(model_dir)
    files = write_sysml(design, model_dir)
    stats["sysml_files"] = len(files)
    stats["kpar"] = write_kpar(files, os.path.join(run_dir, re.sub(r"\W+", "_", title).strip("_") + ".kpar"), title)
    summary = {"title": title, "components": len(design["components"]), "subsystems": len(design["subsystems"]),
               "actors": len(design["actors"]), "connections": len(design["connections"]), "routes": len(design["routes"]),
               "state_machines": len(design["state_machines"]), "activities": len(design["activities"]),
               "interactions": len(design["interactions"]), "requirements": len(atomic), "left_out": len(left_out), **stats}
    return summary


__all__ = ["read_design", "write_design", "supported", "title_for"]
