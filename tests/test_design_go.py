"""The system design read from a Go service (tests/fixtures/go_service): parts from the wiring,
connections from injected fields, external systems from client libraries, routes with their mount
prefix, the status state machine, background work, settings, and the document's sections."""
import json
import os
import re

import pytest

from code2req.design import fix_route_text, title_for, write_design
from code2req.design.golang import Design
from code2req.design.render import render

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "go_service")


def report():
    rel = "api/handler.go"
    with open(os.path.join(FIXTURE, rel), encoding="utf-8") as f:
        line = next(i for i, ln in enumerate(f.read().splitlines(), 1) if 'HandleFunc("POST /orders"' in ln)
    return {"surfaces": [{"kind": "http_route", "name": "POST /orders", "file": rel, "line": line}],
            "requirements": [
                {"id": "CODE-ORDERS-001", "type": "functional", "evidence": [{"file": "orders/orders.go", "start_line": 33, "end_line": 39}],
                 "text": "The order service shall store a new order with status new and charge it in the background."},
                {"id": "CODE-API-001", "type": "interface", "evidence": [{"file": "api/handler.go", "start_line": 21, "end_line": 33}],
                 "text": "The API shall accept POST /orders and respond 201 with the order."}],
            "stats": {}}


@pytest.fixture(scope="module")
def design():
    return Design(FIXTURE, report(), "Orders").build()


def test_parts_are_what_the_wiring_builds(design):
    parts = {p["name"] for c in design["components"] for p in c["parts"]}
    assert {"ordersRepository", "paymentsRepository", "ordersService", "gateway", "apiHandler", "httpRouter"} <= parts
    repo = next(c for c in design["components"] if len(c["parts"]) == 2)
    assert [p["over"] for p in repo["parts"]] == ["orders", "payments"]


def test_connections_follow_injected_fields_and_client_libraries(design):
    pairs = {(c["from"], c["to"]) for c in design["connections"]}
    assert {("apiClient", "httpRouter"), ("httpRouter", "apiHandler"), ("apiHandler", "ordersService"),
            ("ordersService", "ordersRepository"), ("ordersService", "paymentsRepository"), ("ordersService", "gateway"),
            ("gateway", "stripe"), ("ordersRepository", "database"), ("paymentsRepository", "database")} <= pairs
    place = next(c for c in design["connections"] if (c["from"], c["to"]) == ("apiHandler", "ordersService"))
    assert place["calls"][0]["name"] == "Place" and place["calls"][0]["args"] == ["id: String", "amount: Integer"]
    assert {a["name"] for a in design["actors"]} == {"ApiClient", "Database", "Stripe"}


def test_routes_carry_the_mount_prefix_and_what_they_read(design):
    (r,) = design["routes"]
    assert (r["method"], r["path"], r["handler"]) == ("POST", "/api/orders", "apiHandler.Place")
    assert r["codes"] == [201, 400, 500]
    assert "id: String (JSON body `placeRequest`)" in r["request"]


def test_the_status_type_is_a_state_machine(design):
    (m,) = design["state_machines"]
    assert m["owner"] == "OrdersService" and m["initial"]["state"] == "statusNew"
    assert [(t["from"], t["to"]) for t in m["transitions"]] == [("statusNew", "statusFailed"), ("statusNew", "statusPaid")]
    assert "Charge fails" in m["transitions"][0]["when"] and "Charge succeeds" in m["transitions"][1]["when"]
    assert m["transitions"][1]["effect"] == "ordersRepository.SetStatus saves status statusPaid"


def test_background_work_is_an_activity_and_an_interaction(design):
    charge = next(a for a in design["activities"] if a["name"] == "charge")
    assert charge["background"]
    place = next(a for a in design["activities"] if a["name"] == "Place" and a["owner"] == "OrdersService")
    assert any("in the background" in s.get("do", "") for s in place["steps"])
    seq = next(s for s in design["interactions"] if s["name"] == "ChargeInBackground")
    assert [(m["from"], m["to"]) for m in seq["messages"]][:2] == [("ordersService", "gateway"), ("gateway", "stripe")]


def test_settings_come_from_the_environment(design):
    s = {x["env"]: x for x in design["settings"]}
    assert s["PORT"]["value"] == '"8080"' and s["STRIPE_KEY"]["owner"] == "Gateway"


def test_the_document_answers_each_question_in_one_part(design):
    md = render(dict(design, requirements=[], left_out_requirements=[], source=FIXTURE))
    for h in ("## 2. Pieces", "## 3. Connections", "## 5. Routes", "## 8. State machines", "## 9. Activities", "## 10. Interactions"):
        assert h in md
    assert max(len(s) for s in re.split(r"\n(?=#{2,3} )", md)) < 12000
    assert "MI-0" not in md and "## 0." not in md


def test_settings_requirements_and_returns_are_written_in_the_forms_the_modeller_keeps(design):
    reqs = [{"id": "CODE-ORDERS-001", "name": "StoreNewOrder", "kind": "functional", "subject": "OrdersService",
             "implemented_by": "OP_02", "text": "The OrdersService component shall store a new order."}]
    md = render(dict(design, requirements=reqs, left_out_requirements=[], source=FIXTURE))
    assert '`portValue : String = "8080"`' in md  # `port` is a SysML keyword
    assert "`requirement def <'CODE-ORDERS-001'> StoreNewOrder`" in md and "not extracted again" in md
    place = next(a for a in design["activities"] if a["name"] == "Place" and a["owner"] == "OrdersService")
    assert any(s.get("text") == "Return &o." for s in place["steps"])
    handler = next(a for a in design["activities"] if a["name"] == "Place" and a["owner"] == "ApiHandler")
    assert "Stop and return the error." not in str(handler["steps"])


def test_route_text_is_checked_against_the_routes(design):
    text, fixes = fix_route_text("The API component shall accept POST /orders.", design, "ApiHandler", "")
    assert text == "The API component shall accept POST /api/orders." and fixes == [("POST /orders", "POST /api/orders")]


def test_title_is_the_repo_name():
    assert title_for("d:/work/pearson-specter/backend") == "Pearson Specter"
    assert title_for("", "https://github.com/acme/order-service.git") == "Order Service"


class EchoLLM:
    """Answers the atomizer with each requirement as it is: one statement, its own ID."""

    def complete(self, system, user, json_mode=False):
        reqs = json.loads(user)["requirements"]
        return json.dumps({"requirements": [{"id": r["id"], "source": r["id"], "name": f"Req{i}", "text": r["text"],
                                             "kind": "functional", "attributes": []} for i, r in enumerate(reqs, 1)]})


def test_write_design_writes_the_documents(tmp_path):
    with open(tmp_path / "requirements.json", "w", encoding="utf-8") as f:
        json.dump(report(), f)
    s = write_design(str(tmp_path), FIXTURE, EchoLLM(), title="Orders")
    assert s["components"] >= 5 and s["requirements"] == 2
    with open(tmp_path / "design.json", encoding="utf-8") as f:
        d = json.load(f)
    subject = {r["id"]: r["subject"] for r in d["requirements"]}
    assert subject == {"CODE-ORDERS-001": "OrdersService", "CODE-API-001": "ApiHandler"}
    assert "POST /api/orders" in next(r["text"] for r in d["requirements"] if r["id"] == "CODE-API-001")
    assert (tmp_path / "system_design.txt").read_text(encoding="utf-8") == (tmp_path / "system_design.md").read_text(encoding="utf-8")
