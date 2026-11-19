"""bspec/compare.py: two SysML v2 models matched element by element, deterministically."""
from code2req.bspec.compare import compare, markdown, read_texts

GOLDEN = """package V {
  part def VendingMachine { attribute coins = (5, 10, 25); attribute tempRange = 2..8; attribute motorTries = 3; }
  part def CoinSlot; part def Customer;
  requirement def <'VM-1'> AcceptCoins { doc /* accept only coins in coins; push any other coin back */ }
  requirement def <'VM-7'> TempOut { doc /* while temperature is outside tempRange, refuse coins and selections and return any credit */ }
  item def InsertCoin; item def Select; item def Cancel; item def TempOutside; item def TempInRange;
  state def Controller {
    entry; then Idle;
    state Idle; state Credited; state OutOfService;
    transition first Idle accept InsertCoin if valid do addCredit then Credited;
    transition first Credited accept Cancel do returnCredit then Idle;
    transition first Idle accept TempOutside then OutOfService;
    transition first Credited accept TempOutside do returnCredit then OutOfService;
    transition first OutOfService accept TempInRange then Idle;
  }
  use case def BuyItem { subject vm : VendingMachine; actor customer : Customer;
    action insertCoin; action select; action collectItem { @DocOnly; } }
}"""

GENERATED = """package M {
  part def Machine { attribute COIN_VALUES = (5, 10, 25); attribute TEMP_LOW_C = 2.0; attribute TEMP_HIGH_C = 8.0; attribute MOTOR_TRIES = 3; }
  part def Customer;
  requirement def <'R1'> Coins { doc /* reject a coin not in the accepted coin values and push it back */ }
  state def Mode {
    entry; then IDLE;
    state IDLE; state CREDITED; state FAULT;
    transition first IDLE accept CoinInserted if accepted then CREDITED;
    transition first CREDITED accept CancelPressed then IDLE;
    transition first IDLE accept TemperatureOutOfRange then FAULT;
    transition first CREDITED accept TemperatureOutOfRange then FAULT;
    transition first FAULT accept TemperatureBackInRange then IDLE;
  }
  use case def Purchase { subject m : Machine; actor c : Customer; action insertCoin; action select; }
}"""


def result():
    a, b = read_texts({"a": GOLDEN}, "golden"), read_texts({"b": GENERATED}, "generated")
    return a, b, compare(a, b)


def test_a_state_with_another_name_matches_by_its_transitions_and_is_listed():
    _, _, res = result()
    by = {m.a.name: m for m in res["state"].matches}
    assert by["Idle"].rule == "name" and by["Credited"].rule == "name"
    assert by["OutOfService"].b.name == "FAULT" and by["OutOfService"].rule == "transitions"
    assert "name differs: OutOfService vs FAULT" in by["OutOfService"].notes


def test_transitions_match_on_their_ends_and_report_what_differs():
    _, _, res = result()
    r = res["transition"]
    assert r.recall == 1.0 and r.precision == 1.0
    notes = {m.a.label(): m.notes for m in r.matches}
    assert any("effect differs: 'returnCredit'" in n for ns in notes.values() for n in ns)
    assert any("guard differs: 'valid' vs 'accepted'" in n for ns in notes.values() for n in ns)


def test_one_setting_written_as_two_is_one_split_match_counted_once():
    _, _, res = result()
    r = res["attribute"]
    assert {m.b.name for m in r.matches if m.rule == "split"} == {"TEMP_LOW_C", "TEMP_HIGH_C"}
    assert (r.n_a, r.matched_a, r.recall) == (3, 3, 1.0) and r.precision == 1.0


def test_unmatched_items_are_listed_both_ways_and_doc_only_steps_apart():
    a, b, res = result()
    assert [e.id for e in res["requirement"].only_a] == ["VM-7"]
    assert [e.name for e in res["part"].only_a] == ["CoinSlot"]  # subjects (the systems) are not parts
    uc = res["usecase"].matches[0]
    assert uc.rule == "actor+steps" and "doc-only steps: collectItem" in uc.notes
    md = markdown(a, b, res)
    assert "only in A: VM-7" in md and "| state | 3 | 3 | 3/3 | 100% | 100% |" in md


def test_output_is_the_same_every_run():
    a, b, res = result()
    assert markdown(a, b, res) == markdown(*result())
