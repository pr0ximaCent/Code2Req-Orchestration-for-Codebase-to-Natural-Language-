"""Scoring the model SysModeler's full model generation builds from a run's requirements."""

import json

from code2req.eval_model import counts, load, pairs, recall, score

KEY = {
    "name": "kiosk",
    "components": [{"name": "Kiosk"}, {"name": "CardReader"}, {"name": "Printer", "aliases": ["receipt_printer"]},
                   {"name": "Scale"}],
    "connections": [["Kiosk", "CardReader"], ["Kiosk", "Printer"], ["Kiosk", "Scale"]],
    "state_machines": [{"owner": "Kiosk", "states": ["IDLE", "PAYING", "PRINTING"], "transitions": 4}],
}


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def fmg_run(tmp_path):
    """A run folder holding a small FMG debug folder: a kiosk system with two parts."""
    debug = tmp_path / "run" / "fmg" / "fmg-debug"
    write(debug / "01_component_breakdown" / "component_breakdown.json", {"components": [
        {"name": "kiosk_system", "kind": "system", "requirements": ["R1", "R2"]},
        {"name": "card_reader", "parent": "kiosk_system", "kind": "component", "requirements": ["R3"]},
        {"name": "receipt_printer", "parent": "kiosk_system", "kind": "component", "requirements": ["R4"],
         "uses": ["card_reader"]}]})
    write(debug / "02_model_plan" / "model_plan.json", {
        "components": [{"name": "kiosk_system", "part_usage": "kiosk"}, {"name": "card_reader", "part_usage": "reader"},
                       {"name": "receipt_printer", "part_usage": "printer"}],
        "requirements": {"R1": {}, "R2": {}, "R3": {}, "R4": {}, "R5": {}}, "unowned": ["R5"]})
    write(debug / "03_component_design" / "designs" / "kiosk_system.json", {
        "ports": [{"name": "cardIn"}],
        "connections": [{"name": "c1", "from": "reader.out", "to": "kiosk.cardIn"}],
        "states": [{"name": "KioskStates", "states": [{"name": "IDLE"}, {"name": "PAYING"}],
                    "transitions": [{"from": "IDLE", "to": "PAYING"}, {"from": "PAYING", "to": "IDLE"}]}]})
    write(debug / "04_interaction_design" / "interactions.json", {
        "actors": [{"name": "customer"}],
        "interactions": [{"name": "Pay", "messages": [{"from": "customer", "to": "kiosk_system"},
                                                      {"from": "kiosk_system", "to": "printer"}]}]})
    return tmp_path / "run"


def test_counts_say_what_each_diagram_will_show(tmp_path):
    fmg = load(str(fmg_run(tmp_path) / "fmg" / "fmg-debug"))
    assert counts(fmg) == {"components": 3, "levels": 2, "attributes": 0, "ports": 1, "connections": 1, "uses": 1,
                           "actions": 0, "state_machines": 1, "states": 2, "transitions": 2, "interactions": 1,
                           "messages": 2, "actors": 1, "requirements": 5, "unowned_requirements": 1}


def test_components_are_linked_by_uses_connections_and_messages_through_their_part_usages(tmp_path):
    fmg = load(str(fmg_run(tmp_path) / "fmg" / "fmg-debug"))
    assert pairs(fmg) == {frozenset(("receipt_printer", "card_reader")), frozenset(("card_reader", "kiosk_system")),
                          frozenset(("kiosk_system", "receipt_printer"))}


def test_recall_matches_names_without_kind_words_or_case_and_through_aliases(tmp_path):
    r = recall(load(str(fmg_run(tmp_path) / "fmg" / "fmg-debug")), KEY)
    assert r["components"]["matched"] == {"Kiosk": "kiosk_system", "CardReader": "card_reader",
                                          "Printer": "receipt_printer"}
    assert r["components"]["missing"] == ["Scale"]
    assert (r["connections"]["found"], r["connections"]["missing"]) == (2, [["Kiosk", "Scale"]])
    assert (r["states"]["found"], r["states"]["missing"]) == (2, ["PRINTING"])
    assert r["transitions"] == {"found": 2, "of": 4}


def test_score_writes_score_json_beside_the_dry_run(tmp_path):
    run = fmg_run(tmp_path)
    result = score(str(run), KEY)
    assert json.loads((run / "fmg" / "score.json").read_text(encoding="utf-8")) == json.loads(json.dumps(result))
    assert result["components"] == ["kiosk_system", "card_reader", "receipt_printer"]


def test_fmg_command_scores_an_existing_dry_run_and_says_when_there_is_none(tmp_path, capsys):
    from code2req.cli import main
    run = fmg_run(tmp_path)
    key = tmp_path / "key.json"
    key.write_text(json.dumps(KEY), encoding="utf-8")
    assert main(["fmg", str(run), "--score-only", "--key", str(key)]) == 0
    out = capsys.readouterr().out
    assert "3 component(s) in 2 level(s)" in out and "components 3/4, connections 2/3, states 2/3" in out
    assert main(["fmg", str(tmp_path / "nowhere"), "--score-only"]) == 1
    assert "no FMG debug folder" in capsys.readouterr().out
