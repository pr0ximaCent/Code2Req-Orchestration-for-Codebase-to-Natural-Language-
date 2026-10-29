"""Local web UI: start runs, watch progress, browse and review requirements.

Runs are stored under runs_dir/<run_id>/ with:
  run.json        status, target, options, progress, summary
  run.log         captured log lines
  review.json     per-requirement verdicts from the UI
  requirements.json / .md / .pdf / ...   pipeline output

One run executes at a time (the pipeline's logging is process-wide). The
server is meant for 127.0.0.1 only: it reads local folders and runs git clone.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from code2req.cache import with_cache
from code2req.config import is_git_url, prune_clone, referenced_files, resolve_repo, rmtree
from code2req.llm import LLM
from code2req.pipeline import run as run_pipeline
from code2req.render_pdf import render_pdf
from code2req.structure import DEFAULT_MODULE_CHARS

logger = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_LOG_LINES = 3000
SOURCE_CONTEXT = 6  # extra lines shown around a citation


class RunRequest(BaseModel):
    target: str = Field(min_length=1)
    exclude: list[str] = Field(default_factory=list)
    workers: int = Field(default=4, ge=1, le=16)
    max_module_chars: int = Field(default=DEFAULT_MODULE_CHARS, ge=5_000, le=400_000)
    followup: bool = True
    entries: list[str] = Field(default_factory=list)  # extra entry-point globs for dead-code detection
    assets: bool = True  # include prompts, templates, reference files and CI/CD
    cache: bool = True  # reuse answers for modules whose code is unchanged
    keep_clone: bool = False  # git URL: keep the whole clone, not only the files the result cites


class ReviewRequest(BaseModel):
    req_id: str
    verdict: str = Field(pattern="^(accepted|wrong|unsure|clear)$")
    note: str = ""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    # Windows refuses to replace a file another thread has open for reading.
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


class _RunLogHandler(logging.Handler):
    """Collects log lines of the active run into memory and run.log."""

    def __init__(self, path: str, sink: list[str]):
        super().__init__(level=logging.INFO)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s",
                                            "%H:%M:%S"))
        self.sink = sink
        self.file = open(path, "a", encoding="utf-8")  # noqa: SIM115 - closed in close()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001 - never let logging break a run
            return
        self.sink.append(line)
        if len(self.sink) > MAX_LOG_LINES:
            del self.sink[: len(self.sink) - MAX_LOG_LINES]
        self.file.write(line + "\n")
        self.file.flush()

    def close(self) -> None:
        self.file.close()
        super().close()


class RunManager:
    def __init__(self, runs_dir: str, llm_factory: Callable[[], tuple[LLM, str]],
                 cache_path: str | None = None):
        self.runs_dir = os.path.abspath(runs_dir)
        self.llm_factory = llm_factory
        self.cache_path = cache_path  # None: the shared default (~/.cache/code2req)
        self.lock = threading.Lock()
        self.active: str | None = None
        self.live_meta: dict[str, dict] = {}  # meta of the active run, served from memory
        self.logs: dict[str, list[str]] = {}
        os.makedirs(self.runs_dir, exist_ok=True)
        self._mark_interrupted()

    def _dir(self, run_id: str) -> str:
        if not run_id or not all(c.isalnum() or c in "-_" for c in run_id):
            raise HTTPException(400, "bad run id")
        path = os.path.join(self.runs_dir, run_id)
        if not os.path.isdir(path):
            raise HTTPException(404, "run not found")
        return path

    def _mark_interrupted(self) -> None:
        """Runs left 'running' by a stopped server can never finish."""
        for run_id in os.listdir(self.runs_dir):
            meta_path = os.path.join(self.runs_dir, run_id, "run.json")
            meta = _read_json(meta_path, None)
            if meta and meta.get("status") in ("queued", "running"):
                meta.update(status="failed", error="server stopped during the run",
                            finished_at=_now())
                _write_json(meta_path, meta)

    def meta(self, run_id: str) -> dict:
        live = self.live_meta.get(run_id)
        if live is not None:
            return dict(live)
        meta = _read_json(os.path.join(self._dir(run_id), "run.json"), None)
        if meta is None:
            raise HTTPException(404, "run metadata missing")
        return meta

    def list_runs(self) -> list[dict]:
        runs = []
        for run_id in os.listdir(self.runs_dir):
            meta = self.live_meta.get(run_id) or _read_json(
                os.path.join(self.runs_dir, run_id, "run.json"), None)
            if meta:
                runs.append(meta)
        return sorted(runs, key=lambda m: m.get("started_at", ""), reverse=True)

    def start(self, req: RunRequest) -> dict:
        with self.lock:
            if self.active is not None:
                raise HTTPException(409, f"run {self.active} is still in progress")
            run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            out_dir = os.path.join(self.runs_dir, run_id)
            os.makedirs(out_dir)
            meta = {
                "id": run_id, "target": req.target.strip().strip('"'), "status": "queued",
                "started_at": _now(), "finished_at": None, "error": None,
                "options": req.model_dump(), "repo_path": None, "model": None,
                "progress": {"stage": "queued", "done": 0, "total": 0}, "summary": None,
            }
            _write_json(os.path.join(out_dir, "run.json"), meta)
            self.active = run_id
            self.live_meta[run_id] = meta
            self.logs[run_id] = []
        threading.Thread(target=self._execute, args=(run_id, req), daemon=True).start()
        return meta

    def _execute(self, run_id: str, req: RunRequest) -> None:
        out_dir = os.path.join(self.runs_dir, run_id)
        meta_path = os.path.join(out_dir, "run.json")
        meta = self.live_meta[run_id]
        handler = _RunLogHandler(os.path.join(out_dir, "run.log"), self.logs[run_id])
        root = logging.getLogger()
        root.addHandler(handler)
        if root.level > logging.INFO or root.level == logging.NOTSET:
            root.setLevel(logging.INFO)

        meta_lock = threading.Lock()  # progress arrives from the pipeline's worker threads

        def progress(stage: str, done: int, total: int) -> None:
            with meta_lock:
                meta["progress"] = {"stage": stage, "done": done, "total": total}
                _write_json(meta_path, meta)

        try:
            meta["status"] = "running"
            progress("resolving", 0, 0)
            repo = resolve_repo(meta["target"], out_dir)
            meta["repo_path"] = repo
            llm, label = self.llm_factory()
            meta["model"] = label
            logger.info("run %s: %s (%s)", run_id, repo, label)
            llm = with_cache(llm, label, enabled=req.cache, path=self.cache_path)
            report = run_pipeline(repo, out_dir, llm, label, req.workers,
                                  req.exclude or None, req.max_module_chars, progress=progress,
                                  followup=req.followup, entries=req.entries or None,
                                  include_assets=req.assets, target=meta["target"])
            if is_git_url(meta["target"]) and not req.keep_clone:
                kept, removed = prune_clone(repo, referenced_files(report))
                logger.info("clone pruned to the %d files the result cites (%d removed)", kept, removed)
            st = report.stats
            meta["summary"] = {
                "requirements": st["requirements"], "references": st["references"], "gaps": st["gaps"],
                "rejected": st["rejected"], "modules": st["modules"],
                "dead_code": len(report.dead_code),
                "failed_modules": len(st.get("failed_modules", [])),
                "coverage": st["surface_coverage"]["ratio"],
                "function_coverage": st["function_coverage"]["ratio"],
                "credits": (st.get("cost") or {}).get("credits"),
                "cost_usd": (st.get("cost") or {}).get("cost_usd"),
                "provider_cost_usd": (st.get("cost") or {}).get("provider_cost_usd"),
                "calls": (st.get("cost") or {}).get("calls"),
                "reused": ((st.get("cost") or {}).get("cache") or {}).get("hits"),
            }
            meta["status"] = "done"
        except Exception as e:  # noqa: BLE001 - report any failure to the UI
            logger.exception("run %s failed", run_id)
            meta["status"] = "failed"
            meta["error"] = getattr(e, "stderr", None) or str(e)
        finally:
            with meta_lock:
                meta["finished_at"] = _now()
                _write_json(meta_path, meta)
            root.removeHandler(handler)
            handler.close()
            with self.lock:
                self.active = None
                self.live_meta.pop(run_id, None)

    def log(self, run_id: str, since: int) -> dict:
        lines = self.logs.get(run_id)
        if lines is None:
            path = os.path.join(self._dir(run_id), "run.log")
            try:
                with open(path, encoding="utf-8") as f:
                    lines = f.read().splitlines()
            except OSError:
                lines = []
        return {"lines": lines[since:], "next": len(lines)}

    def result(self, run_id: str) -> dict:
        path = os.path.join(self._dir(run_id), "requirements.json")
        if not os.path.isfile(path):
            raise HTTPException(404, "no result yet")
        return _read_json(path, {})

    def source(self, run_id: str, file: str, start: int, end: int) -> dict:
        repo = self.meta(run_id).get("repo_path")
        if not repo:
            raise HTTPException(404, "run has no repo")
        root = os.path.realpath(repo)
        path = os.path.realpath(os.path.join(root, file))
        try:
            inside = os.path.commonpath([root, path]) == root
        except ValueError:  # different drives on Windows
            inside = False
        if not inside or not os.path.isfile(path):
            raise HTTPException(404, "file not found in repo")
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        lo = max(1, start - SOURCE_CONTEXT)
        hi = min(len(lines), max(end, start) + SOURCE_CONTEXT)
        return {"file": file, "start": start, "end": end, "first_line": lo,
                "lines": lines[lo - 1:hi], "total_lines": len(lines)}

    def delete(self, run_id: str) -> None:
        path = self._dir(run_id)
        with self.lock:
            if self.active == run_id:
                raise HTTPException(409, "this run is still in progress; wait for it to finish")
            self.logs.pop(run_id, None)
            rmtree(path)  # a kept clone has read-only git files

    def reviews(self, run_id: str) -> dict:
        return _read_json(os.path.join(self._dir(run_id), "review.json"), {})

    def set_review(self, run_id: str, req: ReviewRequest) -> dict:
        path = os.path.join(self._dir(run_id), "review.json")
        with self.lock:
            data = _read_json(path, {})
            if req.verdict == "clear":
                data.pop(req.req_id, None)
            else:
                data[req.req_id] = {"verdict": req.verdict, "note": req.note.strip(),
                                    "at": _now()}
            _write_json(path, data)
        return data


def create_app(runs_dir: str, llm_factory: Callable[[], tuple[LLM, str]],
               cache_path: str | None = None) -> FastAPI:
    app = FastAPI(title="code2req", docs_url="/api/docs", redoc_url=None)
    mgr = RunManager(runs_dir, llm_factory, cache_path)
    app.state.manager = mgr

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        # no-store: a stale cached page after an update looks like the update failed
        return FileResponse(os.path.join(STATIC_DIR, "index.html"),
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/runs")
    def list_runs() -> list[dict]:
        return mgr.list_runs()

    @app.post("/api/runs")
    def start_run(req: RunRequest) -> dict:
        return mgr.start(req)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        return mgr.meta(run_id)

    @app.delete("/api/runs/{run_id}")
    def delete_run(run_id: str) -> dict:
        mgr.delete(run_id)
        return {"deleted": run_id}

    @app.get("/api/runs/{run_id}/log")
    def get_log(run_id: str, since: int = 0) -> dict:
        return mgr.log(run_id, max(0, since))

    @app.get("/api/runs/{run_id}/result")
    def get_result(run_id: str) -> dict:
        return mgr.result(run_id)

    @app.get("/api/runs/{run_id}/source")
    def get_source(run_id: str, file: str, start: int = 1, end: int = 1) -> dict:
        return mgr.source(run_id, file, max(1, start), max(1, end))

    @app.get("/api/runs/{run_id}/reviews")
    def get_reviews(run_id: str) -> dict:
        return mgr.reviews(run_id)

    @app.post("/api/runs/{run_id}/reviews")
    def set_review(run_id: str, req: ReviewRequest) -> dict:
        return mgr.set_review(run_id, req)

    def reviewed(run_id: str) -> dict:
        """Requirements with the UI verdict attached, for feedback or downstream use."""
        result, reviews = mgr.result(run_id), mgr.reviews(run_id)
        for key in ("requirements", "references", "gaps"):
            for r in result.get(key, []):
                if r.get("id") in reviews:
                    r["review"] = reviews[r["id"]]
        return result

    @app.get("/api/runs/{run_id}/export")
    def export(run_id: str) -> JSONResponse:
        headers = {"Content-Disposition": f'attachment; filename="code2req-{run_id}.json"'}
        return JSONResponse(reviewed(run_id), headers=headers)

    @app.get("/api/runs/{run_id}/export.pdf")
    def export_pdf(run_id: str) -> Response:
        """The same content as the JSON export, as a readable document. Rendered on request
        so verdicts given in the UI since the run are in it."""
        try:
            pdf = render_pdf(reviewed(run_id))
        except Exception as e:
            raise HTTPException(500, f"PDF rendering failed: {e}") from e
        headers = {"Content-Disposition": f'attachment; filename="code2req-{run_id}.pdf"'}
        return Response(pdf, media_type="application/pdf", headers=headers)

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "active_run": mgr.active, "time": time.time()}

    return app
