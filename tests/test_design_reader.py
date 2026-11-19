"""The design reader that works for any language (reader.py, assemble.py): a model answers, code checks,
assembly builds the design. A fake model gives the answers for the Go fixture (tests/fixtures/go_service),
including wrong ones the checks must drop."""
import copy
import json
import os
import re

import pytest

from code2req.design.reader import BEHAVIOUR_SYSTEM, MAP_SYSTEM, Reader, base_type, norm_actor, number_text, source_files
from code2req.design.render import steps_lines

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "go_service")

MAP = {
    "api/handler.go": {
        "components": [{"name": "Handler", "file": "F1", "lines": [10, 12], "kind": "handler", "purpose": "Serves the order routes",
                        "given": [{"name": "svc", "type": "*orders.Service", "line": 11}],
                        "operations": [{"name": "Place", "lines": [21, 33], "inputs": ["w: ResponseWriter"], "output": ""}]},
                       {"name": "Phantom", "file": "F1", "lines": [1, 2], "kind": "service", "operations": []}],
        "data": [{"name": "placeRequest", "file": "F1", "lines": [16, 19], "fields": ["id: string", "amount: int"]}],
        "routes": [{"method": "POST", "path": "/orders", "component": "Handler", "operation": "Place", "file": "F1", "line": 42}]},
    "app/app.go": {
        "components": [{"name": "svc", "file": "F1", "lines": [18, 18], "kind": "service", "operations": []}],
        "wiring": [{"instance": "ordersRepo", "component": "store.NewRepository", "over": "orders", "passed": {"db": "db"}, "file": "F1", "line": 15},
                   {"instance": "paymentsRepo", "component": "store.NewRepository", "over": "payments", "passed": {"db": "db"}, "file": "F1", "line": 16},
                   {"instance": "gateway", "component": "orders.NewGateway", "passed": {}, "file": "F1", "line": 17},
                   {"instance": "svc", "component": "orders.NewService", "file": "F1", "line": 18,
                    "passed": {"ordersRepo": "ordersRepo", "paymentsRepo": "paymentsRepo", "gateway": "gateway"}},
                   {"instance": "h", "component": "api.NewHandler", "passed": {"svc": "svc"}, "file": "F1", "line": 19}],
        "mounts": [{"prefix": "/api", "file": "F1", "line": 28}]},
    "config/config.go": {
        "settings": [{"key": "PORT", "default": "8080", "field": "Port", "file": "F1", "line": 22},
                     {"key": "DATABASE_URL", "default": "postgres://localhost/orders", "field": "DB.URL", "file": "F1", "line": 24},
                     {"key": "STRIPE_KEY", "default": "sk_test", "field": "Stripe.Key", "file": "F1", "line": 27, "owner": "OrdersGateway"},
                     {"key": "cfg.Port", "default": "", "field": "Port", "file": "F1", "line": 14}]},
    "orders/gateway.go": {  # F1 gateway.go, F2 orders.go
        "components": [{"name": "Gateway", "file": "F1", "lines": [10, 12], "kind": "client", "purpose": "Charges cards through Stripe",
                        "given": [{"name": "api", "type": "*client.API", "line": 11}],
                        "operations": [{"name": "Charge", "lines": [18, 21]}, {"name": "Refund", "lines": [24, 40]}],
                        "external": [{"system": "Stripe", "client": "client.API", "line": 19}]},
                       {"name": "Service", "file": "F2", "lines": [26, 30], "kind": "service", "purpose": "Takes orders and charges them",
                        "given": [{"name": "orders", "type": "*store.Repository", "line": 27},
                                  {"name": "payments", "type": "*store.Repository", "line": 28},
                                  {"name": "gateway", "type": "*Gateway", "line": 29}],
                        "operations": [{"name": "Place", "lines": [37, 44]}, {"name": "charge", "lines": [46, 53], "background": True},
                                       {"name": "Reconcile", "lines": [56, 83]}]}],
        "enums": [{"name": "Status", "file": "F2", "lines": [11, 17], "status": True,
                   "values": [{"name": "StatusNew", "value": "new"}, {"name": "StatusPaid", "value": "paid"},
                              {"name": "StatusFailed", "value": "failed"}]}],
        "data": [{"name": "Order", "file": "F2", "lines": [19, 23], "fields": ["id: string", "amount: int", "status: Status"]}]},
    "store/store.go": {
        "components": [{"name": "Repository", "file": "F1", "lines": [6, 9], "kind": "repository", "purpose": "Keeps the rows of one table",
                        "given": [{"name": "db", "type": "*sql.DB", "line": 7}],
                        "operations": [{"name": "Insert", "lines": [15, 18]}, {"name": "SetStatus", "lines": [20, 23]}],
                        "external": [{"system": "SQL database", "client": "sql.DB", "line": 16}]}]},
    "main.go": {"entrypoints": [{"name": "main", "file": "F1", "lines": [12, 20]}]},
}

BEHAVIOUR = {
    "Handler": {"operations": [{"name": "Place", "steps": [
        {"if": "Decode fails", "line": 23, "then": [{"respond": 400, "line": 24}, {"return": "", "line": 25}], "else": []},
        {"call": "h.svc.Place", "to": "Service", "op": "Place", "args": "req.ID, req.Amount", "line": 27},
        {"if": "Place fails", "line": 28, "then": [{"respond": 500, "line": 29}, {"return": "", "line": 30}], "else": []},
        {"respond": 201, "line": 32}]}]},
    "OrdersService": {"operations": [
        {"name": "Place", "steps": [
            {"call": "s.orders.Insert", "to": "Repository", "op": "Insert", "args": "o.ID, new", "line": 39},
            {"if": "Insert fails", "line": 39, "then": [{"return": "error", "line": 40}], "else": []},
            {"background": "s.charge(o)", "line": 42},
            {"return": "the order", "line": 43}]},
        {"name": "charge", "steps": [
            {"call": "s.gateway.Charge", "to": "Gateway", "op": "Charge", "args": "o.ID, o.Amount", "line": 47},
            {"if": "Charge fails", "line": 47, "then": [{"call": "s.orders.SetStatus", "to": "Repository", "op": "SetStatus",
                                                         "args": "o.ID, failed", "line": 48}, {"return": "", "line": 49}], "else": []},
            {"call": "s.payments.Insert", "to": "Repository", "op": "Insert", "args": "o.ID, ok", "line": 51},
            {"return": "", "line": 51},  # wrong: line 51 is no return
            {"call": "s.orders.SetStatus", "to": "Repository", "op": "SetStatus", "args": "o.ID, paid", "line": 52}]},
        {"name": "Reconcile", "steps": [
            {"loop": "each id in ids", "line": 59, "steps": [
                {"if": "the context is cancelled", "line": 60, "then": [{"return": "error", "line": 61}], "else": []},
                {"if": "id was seen", "line": 63, "then": [{"skip": "id", "line": 64}], "else": []},
                {"if": "id is not empty", "line": 67, "then": [
                    {"call": "s.gateway.Charge", "to": "Gateway", "op": "Charge", "args": "id, 0", "line": 68},
                    {"if": "Charge fails", "line": 69, "then": [{"log": "charge failed", "level": "warning", "line": 70},
                                                                {"skip": "id", "line": 71}], "else": []}],
                 "else": [{"skip": "id", "line": 75}]},
                {"call": "s.payments.Insert", "to": "Repository", "op": "Insert", "args": "id, ok", "line": 77},
                {"if": "Insert fails", "line": 77, "then": [{"log": "payment not recorded", "level": "warning", "line": 78}], "else": []}]},
            {"return": "done", "line": 82}]}],
        "transitions": [{"status": "Status", "to": "StatusNew", "operation": "Place", "when": "a new order is stored", "line": 38},
                        {"status": "Status", "from": "StatusNew", "to": "StatusFailed", "operation": "charge", "when": "the charge fails", "line": 48},
                        {"status": "Status", "from": "StatusNew", "to": "StatusPaid", "operation": "charge", "when": "the charge succeeds", "line": 52}]},
    "OrdersGateway": {"operations": [
        {"name": "Charge", "steps": [{"call": "g.api.PaymentIntents.New", "to": "Stripe", "op": "New", "args": "nil", "line": 19},
                                     {"return": "error", "line": 20}]},
        {"name": "Refund", "steps": [{"switch": "currency", "line": 26, "cases": [
            {"case": "usd", "steps": [{"set": "ref = \"usd-refund\"", "line": 30}]},
            {"case": "eur", "steps": [{"call": "g.api.Refunds.New", "to": "Stripe", "op": "New", "args": "nil", "line": 32},
                                      {"if": "New fails", "line": 32, "then": [{"return": "error", "line": 33}], "else": []}]},
            {"case": "otherwise", "steps": [{"log": "unknown currency", "level": "warning", "line": 37}]}]},
            {"return": "ref", "line": 39}]}]},
    "StoreRepository": {"operations": [
        {"name": "Insert", "steps": [{"call": "r.db.Exec", "to": "SQL database", "op": "Exec", "args": "INSERT", "line": 16},
                                     {"return": "error", "line": 17}]},
        {"name": "SetStatus", "steps": [{"call": "r.db.Exec", "to": "SQL database", "op": "Exec", "args": "UPDATE", "line": 21},
                                        {"return": "error", "line": 22}]}]},
    "Orders": {"operations": [{"name": "main", "steps": [
        {"call": "sql.Open", "to": "SQL database", "op": "Open", "args": "postgres", "line": 14},
        {"if": "Open fails", "line": 15, "then": [{"return": "exit", "line": 16}], "else": []},
        {"call": "app.Build", "to": "SELF", "op": "Build", "args": "cfg, db", "line": 18},
        {"call": "http.ListenAndServe", "to": "SELF", "op": "ListenAndServe", "args": "port, h", "line": 19}]}]},
}


class FakeModel:
    """Answers the map with MAP for the files a prompt lists, and the behaviour with BEHAVIOUR for its component."""

    def __init__(self, map_=None):
        self.calls = []
        self.map = map_ or MAP

    def complete(self, system, user, json_mode=False):
        self.calls.append(system[:20])
        if system == MAP_SYSTEM:
            first = re.search(r"### F1 (\S+)", user).group(1)
            return json.dumps(self.map.get(first, {}))
        if system == BEHAVIOUR_SYSTEM:
            name = re.search(r"COMPONENT (.+)", user).group(1).strip()
            return json.dumps(BEHAVIOUR.get(name, {}))
        raise AssertionError("unexpected prompt")


@pytest.fixture(scope="module")
def reader():
    r = Reader(FIXTURE, {"surfaces": [{"kind": "http_route", "name": "POST /orders", "file": "api/handler.go", "line": 42}]},
               "Orders", FakeModel(), source_files(FIXTURE), workers=1)
    r.design = r.build()
    return r


def test_the_checks_drop_what_is_not_in_the_code(reader):
    names = {c["name"] for c in reader.design["components"]}
    assert names == {"Handler", "OrdersService", "OrdersGateway", "StoreRepository", "HttpRouter"}  # no Phantom, no svc
    # a generic class name takes its folder in front, as two Handlers in two folders must
    assert any("line 51 does not return" in p for p in reader.problems)
    charge = next(a for a in reader.design["activities"] if a["name"] == "charge")
    assert "Stop" not in "\n".join(steps_lines(charge["steps"])).split("Call payments")[1].split("SetStatus")[0]
    assert "cfg.Port" not in {s["env"] for s in reader.design["settings"]}  # a field read, not a setting


def test_parts_connections_and_routes_come_from_the_wiring(reader):
    d = reader.design
    parts = {p["name"]: c["name"] for c in d["components"] for p in c["parts"]}
    assert {"ordersRepo", "paymentsRepo", "gateway", "svc", "h", "httpRouter"} <= set(parts)
    assert parts["ordersRepo"] == parts["paymentsRepo"] == "StoreRepository"
    pairs = {(c["from"], c["to"]) for c in d["connections"]}
    assert {("apiClient", "httpRouter"), ("httpRouter", "h"), ("h", "svc"), ("svc", "ordersRepo"), ("svc", "paymentsRepo"),
            ("svc", "gateway"), ("gateway", "stripe"), ("ordersRepo", "database"), ("paymentsRepo", "database")} <= pairs
    calls = next(c for c in d["connections"] if (c["from"], c["to"]) == ("svc", "ordersRepo"))["calls"]
    assert [x["name"] for x in calls] == ["Insert", "SetStatus"]
    (rt,) = d["routes"]
    assert (rt["method"], rt["path"], rt["handler"], rt["codes"]) == ("POST", "/api/orders", "h.Place", [201, 400, 500])


def test_behaviour_says_what_happens_on_each_failure(reader):
    d = reader.design
    rec = "\n".join(steps_lines(next(a for a in d["activities"] if a["name"] == "Reconcile")["steps"]))
    assert "For each id in ids:" in rec
    assert "If Insert fails (returns an error):" in rec and 'Log a warning ("payment not recorded")' in rec
    assert "Go on; the failure does not end Reconcile." in rec
    place = "\n".join(steps_lines(next(a for a in d["activities"] if a["name"] == "Place" and a["owner"] == "Handler")["steps"]))
    assert place.startswith("1. Receive the request (POST /api/orders).") and "Respond 400." in place
    svc_place = next(a for a in d["activities"] if a["name"] == "Place" and a["owner"] == "OrdersService")
    assert any(s.get("background") == "charge" for s in svc_place["steps"])


def test_states_sequences_settings_and_startup(reader):
    d = reader.design
    (sm,) = d["state_machines"]
    assert sm["initial"]["state"] == "statusNew"
    assert {(t["from"], t["to"]) for t in sm["transitions"]} == {("statusNew", "statusFailed"), ("statusNew", "statusPaid")}
    assert "ordersRepo.SetStatus saves status statusPaid" in {t["effect"] for t in sm["transitions"]}
    seq = next(s for s in d["interactions"] if s["trigger"] == "POST /api/orders")
    assert [(m["from"], m["to"]) for m in seq["messages"]][:5] == [
        ("apiClient", "httpRouter"), ("httpRouter", "h"), ("h", "svc"), ("svc", "ordersRepo"), ("ordersRepo", "database")]
    assert any(s["name"] == "ChargeInBackground" for s in d["interactions"])
    owners = {s["env"]: s["owner"] for s in d["settings"]}
    assert owners["STRIPE_KEY"] == "OrdersGateway" and owners["PORT"] == "Orders"
    assert any(a["name"] == "Startup" and a["owner"] == "Orders" for a in d["activities"])


def test_a_function_that_registers_routes_is_wiring_and_a_subtree_route_a_mount():
    # the model may call the function that mounts the API a router, and its subtree pattern a route
    map_ = copy.deepcopy(MAP)
    app = map_["app/app.go"]
    app["components"].append({"name": "Router", "file": "F1", "lines": [24, 30], "kind": "router",
                              "operations": [{"name": "Router", "lines": [24, 30]}]})
    app["routes"] = [{"method": "ANY", "path": "/api/", "component": "Router", "operation": "Router", "file": "F1", "line": 28}]
    del app["mounts"]
    r = Reader(FIXTURE, {"surfaces": [{"kind": "http_route", "name": "POST /orders", "file": "api/handler.go", "line": 42}]},
               "Orders", FakeModel(map_), source_files(FIXTURE), workers=1)
    d = r.build()
    assert "Router" not in {c["name"] for c in d["components"]} and "HttpRouter" in {c["name"] for c in d["components"]}
    assert [(rt["method"], rt["path"]) for rt in d["routes"]] == [("POST", "/api/orders")]


def test_names_and_types_are_read_alike_in_every_language():
    assert base_type("*mongobase.Repository[DocumentModel]") == "Repository"
    assert base_type("List[UserDto]") == "UserDto" and base_type("Optional<Foo>") == "Foo"
    assert base_type("readonly svc: UserService") == "UserService"
    assert [norm_actor(x) for x in ("OpenAI/Azure OpenAI", "PostgreSQL (pgx)", "SMTP mail")] == ["OpenAi", "PostgreSql", "MailServer"]
    assert [number_text(x) for x in ("200_000", "20 << 20", "5 * 60", "3")] == ["200000", "20971520", "300", "3"]


def test_the_design_is_written_as_a_model(reader, tmp_path):
    from code2req.design.sysml import Writer
    files = Writer(dict(reader.design, requirements=[])).files()
    text = "\n".join(files.values())
    assert "part def <'BLK_" in text and "interface <'CONN_" in text and "state def <'SM_01'>" in text
    assert "occurrence def <'SEQ_" in text and "action def <'OP_" in text
