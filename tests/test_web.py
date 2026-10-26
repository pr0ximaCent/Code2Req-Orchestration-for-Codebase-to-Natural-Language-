"""Web UI API: start a run on the fixture with a fake LLM, then browse and review it."""

import json
import os
import time

from fastapi.testclient import TestClient

from code2req.web.server import create_app
from tests.test_pipeline import CLAIMS, FixtureLLM

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "shop")


def wait_done(client, run_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        meta = client.get(f"/api/runs/{run_id}").json()
        if meta["status"] in ("done", "failed"):
            return meta
        time.sleep(0.2)
    raise AssertionError("run did not finish")


def make_client(tmp_path):
    app = create_app(str(tmp_path / "runs"), llm_factory=lambda: (FixtureLLM(), "fake/model"))
    return TestClient(app)


def test_index_served(tmp_path):
    r = make_client(tmp_path).get("/")
    assert r.status_code == 200 and "code2req" in r.text


def test_run_browse_review_export(tmp_path):
    client = make_client(tmp_path)
    meta = client.post("/api/runs", json={"target": FIXTURE, "workers": 1}).json()
    done = wait_done(client, meta["id"])
    assert done["status"] == "done", done.get("error")
    assert done["summary"]["requirements"] == 2
    assert done["progress"]["stage"] == "done"
    assert [m["id"] for m in client.get("/api/runs").json()] == [meta["id"]]

    log = client.get(f"/api/runs/{meta['id']}/log").json()
    assert any("surfaces:" in line for line in log["lines"])

    result = client.get(f"/api/runs/{meta['id']}/result").json()
    req = result["requirements"][0]
    ev = req["evidence"][0]

    src = client.get(f"/api/runs/{meta['id']}/source",
                     params={"file": ev["file"], "start": ev["start_line"], "end": ev["end_line"]}).json()
    assert src["first_line"] <= ev["start_line"]
    assert any("def" in line for line in src["lines"])

    reviews = client.post(f"/api/runs/{meta['id']}/reviews",
                          json={"req_id": req["id"], "verdict": "wrong", "note": "  not true "}).json()
    assert reviews[req["id"]]["verdict"] == "wrong" and reviews[req["id"]]["note"] == "not true"
    exported = client.get(f"/api/runs/{meta['id']}/export").json()
    assert exported["requirements"][0]["review"]["verdict"] == "wrong"
    cleared = client.post(f"/api/runs/{meta['id']}/reviews",
                          json={"req_id": req["id"], "verdict": "clear"}).json()
    assert cleared == {}


def test_delete_run(tmp_path):
    client = make_client(tmp_path)
    run_id = client.post("/api/runs", json={"target": FIXTURE, "workers": 1}).json()["id"]
    wait_done(client, run_id)
    assert client.delete(f"/api/runs/{run_id}").json() == {"deleted": run_id}
    assert client.get("/api/runs").json() == []
    assert client.get(f"/api/runs/{run_id}").status_code == 404
    assert not (tmp_path / "runs" / run_id).exists()


def test_running_run_cannot_be_deleted(tmp_path):
    client = make_client(tmp_path)
    mgr = client.app.state.manager
    (tmp_path / "runs" / "20260101-000000-aaaaaa").mkdir(parents=True)
    mgr.active = "20260101-000000-aaaaaa"
    assert client.delete("/api/runs/20260101-000000-aaaaaa").status_code == 409


def test_source_cannot_escape_repo(tmp_path):
    client = make_client(tmp_path)
    run_id = client.post("/api/runs", json={"target": FIXTURE}).json()["id"]
    wait_done(client, run_id)
    for bad in ("../../pyproject.toml", "../test_pipeline.py", "C:/Windows/win.ini"):
        r = client.get(f"/api/runs/{run_id}/source", params={"file": bad})
        assert r.status_code == 404, bad


def test_bad_target_fails_cleanly(tmp_path):
    client = make_client(tmp_path)
    run_id = client.post("/api/runs", json={"target": "d:/definitely/not/here"}).json()["id"]
    meta = wait_done(client, run_id)
    assert meta["status"] == "failed" and "not a directory or git URL" in meta["error"]


def test_bad_run_ids_and_inputs(tmp_path):
    client = make_client(tmp_path)
    assert client.get("/api/runs/..%2F..").status_code in (400, 404)
    assert client.get("/api/runs/nope").status_code == 404
    assert client.post("/api/runs", json={"target": ""}).status_code == 422
    assert client.post("/api/runs/x/reviews", json={"req_id": "a", "verdict": "maybe"}).status_code == 422


def test_interrupted_runs_are_marked_failed(tmp_path):
    runs = tmp_path / "runs" / "20260101-000000-abcdef"
    runs.mkdir(parents=True)
    (runs / "run.json").write_text(json.dumps({"id": runs.name, "status": "running",
                                               "started_at": "x", "target": "t"}))
    client = make_client(tmp_path)
    assert client.get(f"/api/runs/{runs.name}").json()["status"] == "failed"


assert CLAIMS  # shared fixture data
