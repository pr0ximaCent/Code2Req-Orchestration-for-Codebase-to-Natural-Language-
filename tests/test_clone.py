"""Cloning downloads only readable files; a finished clone is cut down to what the result cites."""

import os
import subprocess

from code2req import config
from code2req.config import PRUNED_MARK, clone, prune_clone, resolve_repo


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def make_origin(tmp_path):
    src = tmp_path / "origin"
    for rel, text in {"app/main.py": "print(1)\n", "app/img/logo.png": "png\n", "README.md": "# r\n",
                      "node_modules/x/index.js": "x\n", "app/prompts/p.txt": "prompt\n"}.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(text)
    git("init", "-q", cwd=src)
    git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=src)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init", cwd=src)
    git("config", "uploadpack.allowFilter", "true", cwd=src)  # partial clone over file://
    return src


def files_under(root):
    return sorted(os.path.relpath(os.path.join(d, n), root).replace(os.sep, "/")
                  for d, dirs, names in os.walk(root) if ".git" not in d.split(os.sep) for n in names)


def test_clone_leaves_out_binaries_and_skipped_folders(tmp_path):
    src = make_origin(tmp_path)
    dest = tmp_path / "repo"
    clone(src.as_uri(), str(dest))
    assert files_under(dest) == ["README.md", "app/main.py", "app/prompts/p.txt"]


def test_prune_keeps_cited_files_only_and_drops_git(tmp_path):
    src = make_origin(tmp_path)
    dest = tmp_path / "repo"
    clone(src.as_uri(), str(dest))
    kept, removed = prune_clone(str(dest), {"app/main.py"})
    assert (kept, removed) == (1, 2)
    assert files_under(dest) == [PRUNED_MARK, "app/main.py"] and not (dest / ".git").exists()


def test_a_pruned_clone_is_cloned_again(tmp_path, monkeypatch):
    out = tmp_path / "out"
    (out / "repo").mkdir(parents=True)
    (out / "repo" / PRUNED_MARK).write_text("x")
    cloned = []
    monkeypatch.setattr(config, "clone", lambda url, dest: (cloned.append(url), os.makedirs(dest)))
    resolve_repo("https://example.com/shop.git", str(out))
    assert cloned == ["https://example.com/shop.git"] and not (out / "repo" / PRUNED_MARK).exists()
    resolve_repo("https://example.com/shop.git", str(out))  # an unpruned clone is reused
    assert len(cloned) == 1
