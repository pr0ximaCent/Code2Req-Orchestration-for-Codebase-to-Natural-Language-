"""Non-code files that shape behaviour: prompts, skills, templates, reference material, CI/CD.

A text file is included when live code loads it, or when it defines how the
system is built, tested and deployed (CI/CD). Docs (README, CHANGELOG, docs/)
are left out: they state intent, not implementation.

Linking is deterministic. A file is loaded by a code file when a string
literal there names it or one of its folders, directly or through one module
constant (PROMPTS_DIR = BASE_DIR / "templates" / "prompts"). Folders that hold
code are not matched by name, so "tools" or "v2" never link a whole package.
When reachability ran, only live code counts as a loader: a file loaded only
by dead code is dead too.

Each file gets a role, a hint for the asset agent:
  cicd       CI workflows and composite actions, Dockerfiles, compose, Makefiles, deployment manifests
  skill      agent skill files (SKILL.md)
  prompt     LLM prompts and instructions
  template   message and document templates (HTML, Jinja, ...)
  reference  examples, libraries, knowledge bases: captured as reference material
  config     data and settings files the code reads
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field

from code2req.reachability import Reachability
from code2req.schema import DeadCode, Surface
from code2req.surfaces import CODE_EXTS, SKIP_DIRS, WALK_SKIP_DIRS

logger = logging.getLogger(__name__)

TEXT_EXTS = {".txt", ".md", ".markdown", ".html", ".htm", ".jinja", ".jinja2", ".j2", ".tmpl",
             ".mustache", ".prompt", ".sysml", ".kerml", ".json", ".yaml", ".yml", ".toml", ".xml",
             ".sql", ".csv", ".ini", ".cfg", ".graphql", ".proto"}
TEMPLATE_EXTS = {".html", ".htm", ".jinja", ".jinja2", ".j2", ".tmpl", ".mustache"}
CONFIG_EXTS = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml", ".csv"}
REFERENCE_EXTS = {".sysml", ".kerml"}
# Unloaded files of these roles next to loaded ones are dead assets; others (docs, data) are
# only unreferenced.
DEAD_WHEN_UNLOADED = {"prompt", "template", "skill", "reference"}
REFERENCE_DIR_RE = re.compile(r"(example|sample|librar|knowledge|reference|corpus|fixture)", re.I)
DOC_NAMES = re.compile(r"^(readme|changelog|changes|license|licence|contributing|code_of_conduct|"
                       r"security|authors|notice)(\.\w+)?$", re.I)
LOCK_NAMES = {"poetry.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "pipfile.lock",
              "go.sum", "cargo.lock", "composer.lock", "uv.lock"}
ASSET_SKIP_DIRS = {"docs", "doc", "out", "output", "outputs", "tmp", "temp", "logs", "log",
                   "coverage", "htmlcov", "site"}
CICD_DIR_RE = re.compile(r"^(k8s|kubernetes|helm|charts?|deploy|deployments?|deploy(ment)?[-_]files?|"
                         r"manifests|infra|terraform)$", re.I)
MAX_ASSET_BYTES = 1_000_000  # larger text files are data dumps, not behaviour
MIN_LINK_NAME = 3
LITERAL_RE = re.compile(r"""(?:"([^"\n]{1,300})"|'([^'\n]{1,300})')""")
CONST_RE = re.compile(r"^\s*(?:(?:export|public|static|final|const|var|let)\s+)*"
                      r"(_?[A-Z][A-Z0-9_]{2,})\s*(?::[^=\n]+)?(?::=|=)(?!=)(.*)$")
FILE_NAME_RE = re.compile(r"^[\w.-]+\.[A-Za-z]\w{0,5}$")  # "phase1.txt", not "*.txt" or "v2"
LOADER_LINES = 3  # loader locations shown to the asset agent per file

# Reference files longer than this are sent as an outline (head, top-level lines, comments).
REFERENCE_FULL_LINES = 150
REFERENCE_HEAD_LINES = 40
REFERENCE_MAX_LINES = 160
REFERENCE_INDENT = 4


@dataclass
class Asset:
    file: str
    role: str
    lines: list[str]
    loaded_by: list[tuple[str, int, str]] = field(default_factory=list)  # (file, line, code)


@dataclass
class AssetSet:
    assets: list[Asset] = field(default_factory=list)
    dead: list[DeadCode] = field(default_factory=list)
    unreferenced: list[str] = field(default_factory=list)  # text files no code loads (docs etc.)

    def stats(self) -> dict:
        by_role: dict[str, int] = defaultdict(int)
        for a in self.assets:
            by_role[a.role] += 1
        return {"included": len(self.assets), "by_role": dict(sorted(by_role.items())),
                "dead": len(self.dead), "unreferenced": self.unreferenced}


@dataclass
class AssetModule:
    name: str
    path: list[str]
    units: list[tuple[Asset, int, int]]  # (asset, first line, last line)

    @property
    def section(self) -> str:
        return " > ".join(self.path)


def _list_files(repo_path: str) -> list[str]:
    """Tracked files when the repo uses git, else a walk.

    Untracked files are local scratch (debug dumps, one-off scripts), never the
    system: one repo had thousands of untracked debug JSON files next to its code.
    """
    if os.path.isdir(os.path.join(repo_path, ".git")):
        try:
            out = subprocess.run(["git", "ls-files", "--cached"],
                                 cwd=repo_path, capture_output=True, text=True, check=True).stdout
            return sorted({p for p in out.splitlines()
                           if p and os.path.isfile(os.path.join(repo_path, p))})
        except (OSError, subprocess.CalledProcessError) as e:
            logger.warning("git ls-files failed (%s), walking the tree", e)
    files = []
    for root, dirs, names in os.walk(repo_path):
        dirs[:] = sorted(d for d in dirs if d not in WALK_SKIP_DIRS and (not d.startswith(".") or d == ".github"))
        for n in sorted(names):
            files.append(os.path.relpath(os.path.join(root, n), repo_path).replace(os.sep, "/"))
    return files


def is_cicd(rel: str) -> bool:
    parts = rel.split("/")
    base = parts[-1].lower()
    ext = os.path.splitext(base)[1]
    return (rel.startswith((".github/workflows/", ".github/actions/", ".circleci/", ".buildkite/"))
            or base in {".gitlab-ci.yml", "jenkinsfile", "azure-pipelines.yml", "bitbucket-pipelines.yml",
                        "procfile", "compose.yaml", "compose.yml", "makefile", "gnumakefile"}
            or ext == ".mk"
            or base.startswith("dockerfile") or base.endswith(".dockerfile")
            or base.startswith("docker-compose")
            or (ext in {".yaml", ".yml", ".json", ".tf"} and any(CICD_DIR_RE.match(p) for p in parts[:-1])))


def classify(rel: str) -> str:
    if is_cicd(rel):
        return "cicd"
    parts = rel.lower().split("/")
    base = parts[-1]
    ext = os.path.splitext(base)[1]
    if base == "skill.md":
        return "skill"
    if ext in REFERENCE_EXTS or any(REFERENCE_DIR_RE.search(p) for p in parts[:-1]):
        return "reference"
    if any("prompt" in p or "instruction" in p for p in parts) or os.path.splitext(base)[0] == "system":
        return "prompt"
    if ext in TEMPLATE_EXTS or any("template" in p for p in parts[:-1]):
        return "template"
    if ext in CONFIG_EXTS:
        return "config"
    return "prompt"


def _candidate(rel: str, exclude: list[str] | None) -> bool:
    from code2req.verify import is_test_code

    parts = rel.split("/")
    base = parts[-1]
    if is_test_code(rel) or base.lower() in LOCK_NAMES or DOC_NAMES.match(base):
        return False
    if any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(base, p) for p in exclude or []):
        return False
    if is_cicd(rel):
        return True
    if any(p in SKIP_DIRS or p.lower() in ASSET_SKIP_DIRS or p.startswith(".") for p in parts[:-1]):
        return False
    return os.path.splitext(base)[1].lower() in TEXT_EXTS


def _read_lines(repo_path: str, rel: str) -> list[str] | None:
    path = os.path.join(repo_path, rel)
    try:
        if os.path.getsize(path) > MAX_ASSET_BYTES:
            return None
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if b"\x00" in raw[:4096]:
        return None
    return raw.decode("utf-8", errors="replace").splitlines()


# A bare folder name ("simple", "library") is only a link on a line that builds a path.
PATH_CONTEXT_RE = re.compile(r"""(?i)path|_dir\b|\bdir\b|folder|root|/\s*["']|["']\s*/|open\(|glob|"""
                             r"""listdir|scandir|walk\(|read_text|joinpath|resources""")


# How directly a code line loads a file; the agent is shown the most direct loaders first.
# A constant definition naming the file is not a load, so it ranks after the code using it.
RANK_LITERAL, RANK_FOLDER, RANK_CONSTANT, RANK_DEFINITION = 0, 1, 2, 3
IMPORT_RE = re.compile(r"^\s*(from|import|using|require|include)\b|^\s*#\s*include")


def _path_seq(lit: str) -> list[str]:
    """Path pieces of a literal that may name a repo path; [] for URLs, routes, globs, prose."""
    if (lit.startswith("/") or "://" in lit or any(c in lit for c in "{}*?<> %")
            or lit.count(".") > 3):
        return []
    return [p for p in re.split(r"[/\\]", lit) if p and p not in (".", "..")]


class _LinkIndex:
    """Which code lines name a file or folder, directly or through a constant.

    files:   any string-literal piece, matched against file names ("phase1.txt")
    folders: the path a line builds when it loads a whole folder: literals in
             order, with constants expanded (PROMPTS_DIR / "bdd" ->
             templates/prompts/bdd). A folder is linked when that path is a
             suffix of it. Constant definitions, imports and lines naming a
             specific file are not folder loads, so `root=BASE / "knowledge"`
             links every file in knowledge/ while `PROMPTS_DIR / "bdd.txt"`
             links only bdd.txt.
    """

    def __init__(self, repo_path: str, code_files: list[str]):
        # file name -> (path the line builds up to that name, or () when only the bare name, hit)
        self.files: dict[str, list[tuple[tuple[str, ...], tuple[str, int, str]]]] = defaultdict(list)
        self.folders: dict[str, list[tuple[tuple[str, ...], tuple[str, int, str]]]] = defaultdict(list)
        texts: dict[str, list[str]] = {}
        const_lines: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for rel in code_files:
            lines = _read_lines(repo_path, rel)
            if lines:
                texts[rel] = lines
                for i, line in enumerate(lines, 1):
                    c = CONST_RE.match(line)
                    if c and LITERAL_RE.search(line):
                        const_lines[c.group(1)].append((rel, i))
        use_re = (re.compile(r"\b(" + "|".join(map(re.escape, sorted(const_lines, key=len, reverse=True)))
                             + r")\b") if const_lines else None)
        seqs: dict[tuple[str, int], list[str]] = {}

        def def_seq(where: tuple[str, int], depth: int = 0) -> list[str]:
            """The path one constant definition holds, following the constants it is built from."""
            if where in seqs or depth > 5:
                return seqs.get(where, [])
            rel, i = where
            line = texts[rel][i - 1]
            name = CONST_RE.match(line).group(1)
            value = line.split("=", 1)[1] if "=" in line else ""
            seqs[where] = self._seq(value, use_re, lambda n: const_seq(n, rel, depth + 1), skip=name)
            return seqs[where]

        def const_seq(name: str, rel: str, depth: int = 0) -> list[str]:
            """A constant's path, preferring its definition in the same file (names collide)."""
            defs = const_lines[name]
            return def_seq(next((d for d in defs if d[0] == rel), defs[0]), depth)

        for rel, lines in texts.items():
            for i, line in enumerate(lines, 1):
                pieces = {p for m in LITERAL_RE.finditer(line) for p in re.split(r"[/\\]", m.group(1) or m.group(2))
                          if len(p) >= MIN_LINK_NAME}
                defined = CONST_RE.match(line)
                skip = defined.group(1) if defined else ""
                used = [m.group(1) for m in use_re.finditer(line) if m.group(1) != skip] if use_re else []
                direct = set(pieces)
                for name in [] if IMPORT_RE.match(line) else used:
                    # one hop: a constant naming a file links the code using it too
                    pieces |= {p for d in const_lines[name] for p in def_seq(d) if FILE_NAME_RE.match(p)}
                seq = self._seq(line.split("=", 1)[1] if defined else line, use_re,
                                lambda n: const_seq(n, rel), skip=skip)
                for p in pieces:
                    at = len(seq) - 1 - seq[::-1].index(p) if p in seq else -1
                    rank = (RANK_DEFINITION if defined else RANK_LITERAL) if p in direct else RANK_CONSTANT
                    hit = (rel, i, line.strip(), rank)
                    self.files[p].append((tuple(seq[:at + 1]) if at > 0 else (), hit))
                hit = (rel, i, line.strip(), RANK_FOLDER)
                if defined or IMPORT_RE.match(line):
                    continue
                has_literal = LITERAL_RE.search(line) is not None
                if (len(seq) >= 1 and not FILE_NAME_RE.match(seq[-1])
                        and (used or not has_literal or PATH_CONTEXT_RE.search(line)
                             or any(("/" in (m.group(1) or m.group(2)) or "\\" in (m.group(1) or m.group(2)))
                                    for m in LITERAL_RE.finditer(line)))):
                    self.folders[seq[-1]].append((tuple(seq), hit))

    @staticmethod
    def _seq(text: str, use_re, const_seq, skip: str = "") -> list[str]:
        """Path pieces in the order the text builds them, constants expanded."""
        tokens = []
        if use_re:
            tokens += [(m.start(), "c", m.group(1)) for m in use_re.finditer(text) if m.group(1) != skip]
        tokens += [(m.start(), "l", m.group(1) or m.group(2)) for m in LITERAL_RE.finditer(text)]
        seq: list[str] = []
        for _, kind, value in sorted(tokens):
            seq += const_seq(value) if kind == "c" else _path_seq(value)
        return seq

    def loaders(self, rel: str, code_dirs: set[str]) -> list[tuple[str, int, str, int]]:
        """(file, line, code, rank) of lines naming the file or loading a folder of it that holds no code."""
        parts = rel.split("/")
        hits = [hit for seq, hit in self.files.get(parts[-1], [])
                if not seq or tuple(parts[-len(seq):]) == seq]
        for i in range(len(parts) - 1, 0, -1):
            folder = parts[:i]
            if "/".join(folder) in code_dirs:
                continue
            for seq, hit in self.folders.get(folder[-1], []):
                if tuple(folder[-len(seq):]) == seq:
                    hits.append(hit)
        return hits


def discover(repo_path: str, reach: Reachability | None = None,
             exclude: list[str] | None = None, files: list[str] | None = None) -> AssetSet:
    """files: the repo's source list (repofiles); without it, tracked files or a walk."""
    from code2req.verify import is_test_code

    repo_path = os.path.abspath(repo_path)
    files = list(files) if files is not None else _list_files(repo_path)
    # Tests are not loaders in any language: a test reading a fixture does not make it part of the system.
    code_files = [f for f in files if os.path.splitext(f)[1].lower() in CODE_EXTS
                  and not any(p in SKIP_DIRS for p in f.split("/")[:-1]) and not is_test_code(f)]
    # A folder holding only __init__.py is a data folder marked as a package, not code.
    code_dirs = {os.path.dirname(f) for f in code_files if not f.endswith("__init__.py")}
    index = _LinkIndex(repo_path, code_files)

    def live(code_rel: str) -> bool:
        return reach is None or not reach.analysed or code_rel in reach.live_files or not code_rel.endswith(".py")

    out = AssetSet()
    unloaded: list[tuple[str, str, int]] = []
    for rel in files:
        if not _candidate(rel, exclude):
            continue
        lines = _read_lines(repo_path, rel)
        if not lines:
            continue
        role = classify(rel)
        if role == "cicd":
            out.assets.append(Asset(rel, role, lines))
            continue
        hits = index.loaders(rel, code_dirs)
        if not hits:
            unloaded.append((rel, role, len(lines)))
            continue
        live_hits = [h for h in hits if live(h[0])]
        if not live_hits:
            out.dead.append(DeadCode(kind="asset", file=rel, line=1, end_line=len(lines),
                                     reason="loaded only by dead code (" + hits[0][0] + ")"))
            continue
        seen, loaders = set(), []
        for f, i, code, _ in sorted(live_hits, key=lambda h: h[3]):
            if f not in seen:
                seen.add(f)
                loaders.append((f, i, code))
        out.assets.append(Asset(rel, role, lines, loaded_by=loaders[:LOADER_LINES]))

    loaded_dirs = {os.path.dirname(a.file) for a in out.assets if a.role != "cicd"}
    loaded_dirs |= {os.path.dirname(d.file) for d in out.dead}
    for rel, role, n in unloaded:
        if role in DEAD_WHEN_UNLOADED and os.path.dirname(rel) in loaded_dirs:
            out.dead.append(DeadCode(kind="asset", file=rel, line=1, end_line=n,
                                     reason="no code loads this file, though files next to it are loaded"))
        else:
            out.unreferenced.append(rel)
    logger.info("assets: %d included, %d dead, %d unreferenced", len(out.assets), len(out.dead),
                len(out.unreferenced))
    return out


def outline(lines: list[str]) -> list[tuple[int, list[str]]]:
    """Blocks (start line, lines) of a long reference file.

    Its head first, then top-level and comment lines, then lines one level
    deep, until REFERENCE_MAX_LINES: the structure of the whole file rather
    than the first screens of it.
    """
    keep = set(range(1, min(len(lines), REFERENCE_HEAD_LINES) + 1))

    def level(line: str) -> int | None:
        stripped = line.lstrip()
        if not stripped:
            return None
        if len(line) == len(stripped) or stripped.startswith(("//", "#", "/*", "*", "doc ", "comment")):
            return 0
        return 1 if len(line) - len(stripped) <= REFERENCE_INDENT else None

    for wanted in (0, 1):
        for i, line in enumerate(lines, 1):
            if len(keep) >= REFERENCE_MAX_LINES:
                break
            if level(line) == wanted:
                keep.add(i)
    blocks: list[tuple[int, list[str]]] = []
    for i in sorted(keep):
        if blocks and blocks[-1][0] + len(blocks[-1][1]) == i:
            blocks[-1][1].append(lines[i - 1])
        else:
            blocks.append((i, [lines[i - 1]]))
    return blocks


def is_outlined(asset: Asset, start: int, end: int) -> bool:
    return asset.role == "reference" and end - start + 1 > REFERENCE_FULL_LINES


def _unit_chars(asset: Asset, start: int, end: int) -> int:
    if is_outlined(asset, start, end):
        return sum(len(line) + 8 for _, block in outline(asset.lines[start - 1:end]) for line in block)
    return sum(len(line) + 8 for line in asset.lines[start - 1:end])


def _split(asset: Asset, max_chars: int) -> list[tuple[int, int]]:
    """Line ranges of at most max_chars, cut at Markdown headings when possible."""
    n = len(asset.lines)
    if asset.role == "reference" or _unit_chars(asset, 1, n) <= max_chars:
        return [(1, n)]
    ranges, start, size = [], 1, 0
    for i, line in enumerate(asset.lines, 1):
        cost = len(line) + 8
        heading = re.match(r"^\s{0,3}#{1,3}\s", line) is not None
        if size and (size + cost > max_chars or (heading and size > max_chars // 2)):
            ranges.append((start, i - 1))
            start, size = i, 0
        size += cost
    ranges.append((start, n))
    return ranges


CICD_GROUP = "ci-cd"


def _group_key(a: Asset) -> str:
    return CICD_GROUP if a.role == "cicd" else "/".join(a.file.split("/")[:2])


def build_modules(assets: list[Asset], max_chars: int) -> list[AssetModule]:
    """Pack assets into units of at most max_chars, never mixing top-level folders.

    Prompts are often loaded one by one by file name, so grouping by loader
    would cost one model call per small file. Packing by folder keeps related
    files together at a fraction of the calls.
    """
    groups: dict[str, list[Asset]] = defaultdict(list)
    for a in assets:
        groups[_group_key(a)].append(a)
    out: list[AssetModule] = []
    for key in sorted(groups):
        packs: list[list[tuple[Asset, int, int]]] = [[]]
        size = 0
        for a in sorted(groups[key], key=lambda x: x.file):
            for start, end in _split(a, max_chars):
                cost = _unit_chars(a, start, end)
                if packs[-1] and size + cost > max_chars:
                    packs.append([])
                    size = 0
                packs[-1].append((a, start, end))
                size += cost
        for i, units in enumerate(packs, 1):
            if key == CICD_GROUP:
                folder = CICD_GROUP
            else:
                folder = os.path.commonpath([os.path.dirname(a.file) or "." for a, _, _ in units])
                folder = folder.replace(os.sep, "/")
            label = key if folder in (".", "") else folder.rsplit("/", 1)[-1]
            name = label if len(packs) == 1 else f"{label} (part {i})"
            out.append(AssetModule(name=name, path=["assets", folder], units=units))
    return out


def asset_surfaces(assets: list[Asset]) -> list[Surface]:
    """One surface per file, plus one per Markdown section of rule-bearing files."""
    out: list[Surface] = []
    for a in assets:
        out.append(Surface(kind="asset", name=a.file, file=a.file, line=1,
                           end_line=max(1, len(a.lines)), detail=a.role))
        if a.role == "reference" or not a.file.lower().endswith((".md", ".markdown")):
            continue
        heads = [(i, m.group(1).strip()) for i, line in enumerate(a.lines, 1)
                 if (m := re.match(r"^\s{0,3}#{1,3}\s+(.*)$", line))]
        for k, (line, title) in enumerate(heads):
            end = heads[k + 1][0] - 1 if k + 1 < len(heads) else len(a.lines)
            out.append(Surface(kind="asset_section", name=title, file=a.file, line=line,
                               end_line=max(line, end), detail=a.role))
    return out
