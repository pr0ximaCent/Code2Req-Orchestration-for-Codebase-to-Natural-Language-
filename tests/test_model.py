"""The behaviour model: parts, values, states, transitions, activities and sequences read from
the code, checked against the lines they cite and linked to the requirements that cite them."""

import json
import os
import re

from code2req.model import assign_ids, build_model
from code2req.model.checks import check
from code2req.model.mapping import link
from code2req.model.report import markdown
from code2req.model.schema import Part, State, SystemModel, Transition, Value
from code2req.pipeline import run
from code2req.schema import Evidence, FlatRequirement
from code2req.verify import FileCache

# A phone in the shape of device-control code: the system builds its parts, changes state
# through a helper and hands every part one shared log.
PHONE = {
    "phone/__init__.py": "",
    "phone/config.py": (
        "IDLE_TIMEOUT = 30.0     # seconds without input before the phone locks\n"
        "THROTTLE_TEMP = 45.0    # degC: above this the phone throttles\n"),
    "phone/parts.py": '''class Log:
    def __init__(self):
        self.messages = []

    def send(self, sender, receiver, message):
        self.messages.append((sender, receiver, message))


class Display:
    def __init__(self, log):
        self.log = log
        self.phone = None
        self.on = False

    def turn_off(self):
        self.on = False

    def show_lock_screen(self):
        self.on = True

    def show_caller(self):
        self.on = True

    def answer(self):
        self.log.send("Display", "Processor", "answered")
        self.phone.processor.answered()

    def accept_shutter(self, timeout=None):
        return True


class Processor:
    def __init__(self, log):
        self.log = log
        self.phone = None
        self.fast = False

    def full_speed(self):
        self.fast = True

    def wake(self):
        self.phone.display.show_caller()

    def answered(self):
        self.phone.speaker.connect()


class Speaker:
    def __init__(self, log):
        self.log = log

    def connect(self):
        pass


class Camera:
    def __init__(self, log):
        self.log = log

    def stream(self):
        return [1]

    def expose(self):
        return 1

    def save(self, image):
        return 0
''',
    "phone/phone.py": '''from enum import Enum

from . import config
from .parts import Camera, Display, Log, Processor, Speaker


class State(str, Enum):
    OFF = "OFF"
    LOCKED = "LOCKED"
    ACTIVE = "ACTIVE"
    THROTTLED = "THROTTLED"


class Phone:
    def __init__(self, clock):
        self.clock = clock
        self.log = Log()
        self.display = Display(self.log)
        self.processor = Processor(self.log)
        self.speaker = Speaker(self.log)
        self.camera = Camera(self.log)
        self.temp = 25.0
        self.state = None
        self.entered_at = 0.0
        self._enter(State.OFF)

    def _enter(self, state):
        self.state = state
        self.entered_at = self.clock()
        if state is State.OFF:
            self.display.turn_off()
        elif state is State.LOCKED:
            self.display.show_lock_screen()
        elif state is State.ACTIVE:
            self.processor.full_speed()

    def power_pressed(self):
        self.tick()
        if self.state is State.OFF:
            self._enter(State.LOCKED)

    def unlock(self, valid):
        self.tick()
        if self.state is State.LOCKED and valid:
            self._enter(State.ACTIVE)

    def set_temp(self, celsius):
        self.temp = float(celsius)
        if self.state is State.ACTIVE and self.temp > config.THROTTLE_TEMP:
            self._enter(State.THROTTLED)

    def tick(self):
        if self.state is State.ACTIVE and self.clock() - self.entered_at >= config.IDLE_TIMEOUT:
            self._enter(State.LOCKED)
''',
    "phone/camera.py": '''"""Image capture."""
from concurrent.futures import ThreadPoolExecutor


def capture(phone):
    with ThreadPoolExecutor() as pool:
        frames = pool.submit(phone.camera.stream)
        exposure = pool.submit(phone.camera.expose)
        frames.result(), exposure.result()
    if not phone.display.accept_shutter():
        return None
    return phone.camera.save(1)
''',
    "phone/call.py": '''"""Incoming calls, and the outside parties."""


class Network:
    def ring(self, phone):
        phone.log.send("Network", "Processor", "wake")
        phone.processor.wake()


class User:
    def answer(self, phone):
        phone.display.answer()
''',
}

# A rental service in the other common shape: a dataclass holds the status and the service
# changes it after refusing what the rules forbid.
RENTAL = {
    "rental/__init__.py": "",
    "rental/limits.py": "MIN_BALANCE = 3\n",
    "rental/models.py": '''from dataclasses import dataclass
from enum import Enum


class BikeStatus(str, Enum):
    FREE = "free"
    HELD = "held"
    RIDING = "riding"
    BROKEN = "broken"


@dataclass
class Bike:
    bike_id: str
    battery: int = 100
    status: BikeStatus = BikeStatus.FREE
    holder: str = ""


@dataclass
class Rider:
    rider_id: str
    balance: int = 0
''',
    "rental/service.py": '''from typing import Dict

from . import limits
from .models import Bike, BikeStatus, Rider


class Rental:
    def __init__(self):
        self.bikes: Dict[str, Bike] = {}
        self.riders: Dict[str, Rider] = {}

    def hold(self, rider_id, bike_id):
        bike = self.bikes[bike_id]
        if bike.status is not BikeStatus.FREE:
            raise ValueError("bike is not free")
        bike.status = BikeStatus.HELD
        bike.holder = rider_id

    def start(self, rider_id, bike_id):
        rider = self.riders[rider_id]
        bike = self.bikes[bike_id]
        if rider.balance < limits.MIN_BALANCE:
            raise ValueError("balance too low")
        if bike.status is BikeStatus.BROKEN:
            raise ValueError("broken")
        if bike.status is BikeStatus.HELD and bike.holder != rider_id:
            raise ValueError("held by another rider")
        if bike.status is BikeStatus.RIDING:
            raise ValueError("in use")
        bike.status = BikeStatus.RIDING

    def report(self, bike_id, fault):
        bike = self.bikes[bike_id]
        if fault:
            bike.status = BikeStatus.BROKEN

    def finish(self, bike_id):
        self._release(self.bikes[bike_id])

    def _release(self, bike):
        bike.status = BikeStatus.FREE
''',
}


def write(tmp_path, files):
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return str(tmp_path)


def line_of(files, rel, needle):
    return next(i for i, line in enumerate(files[rel].splitlines(), 1) if needle in line)


def req(rid, rel, start, end):
    return FlatRequirement(id=rid, text=f"The phone shall {rid}.",
                           evidence=[Evidence(file=rel, start_line=start, end_line=end)])


def model_of(tmp_path, files, reqs=()):
    repo = write(tmp_path, files)
    return build_model(repo, sorted(f for f in files if f.endswith(".py")), list(reqs), FileCache(repo))


def transitions(model):
    return [(t.source, t.target, t.trigger, t.guard) for t in model.transitions]


def test_the_system_is_the_class_holding_the_most_parts_and_a_shared_log_is_left_out(tmp_path):
    model = model_of(tmp_path, PHONE)
    assert [(p.name, p.kind, p.role, p.parent) for p in model.parts] == [
        ("Phone", "system", "", ""), ("Display", "part", "display", "Phone"),
        ("Processor", "part", "processor", "Phone"), ("Speaker", "part", "speaker", "Phone"),
        ("Camera", "part", "camera", "Phone"), ("Network", "actor", "", ""), ("User", "actor", "", "")]
    values = {(v.owner, v.name): (v.value, v.unit) for v in model.values}
    assert values[("Phone", "IDLE_TIMEOUT")] == ("30.0", "s")
    assert values[("Phone", "THROTTLE_TEMP")] == ("45.0", "°C")
    assert values[("Phone", "temp")] == ("25.0", "")
    assert ("Phone", "entered_at") not in values  # timer bookkeeping, set from the clock


def test_a_helper_driven_state_machine_with_entry_actions_timers_and_change_events(tmp_path):
    model = model_of(tmp_path, PHONE)
    assert [(s.name, s.entry) for s in model.states] == [
        ("OFF", ["Display.turn_off"]), ("LOCKED", ["Display.show_lock_screen"]),
        ("ACTIVE", ["Processor.full_speed"]), ("THROTTLED", [])]
    assert transitions(model) == [
        ("initial", "OFF", "", ""),
        ("OFF", "LOCKED", "power_pressed()", ""),
        ("LOCKED", "ACTIVE", "unlock(valid)", "valid"),
        ("ACTIVE", "THROTTLED", "when(temp > THROTTLE_TEMP)", ""),
        ("ACTIVE", "LOCKED", "after(IDLE_TIMEOUT)", "")]
    assert [t.id for t in model.transitions] == [f"TR_{i:02d}" for i in range(1, 6)]


def test_a_thread_pool_is_a_fork_and_a_flow_a_sequence_draws_is_not_an_activity(tmp_path):
    model = model_of(tmp_path, PHONE)
    assert [a.name for a in model.activities] == ["Capture"]  # not Processor.wake or answered
    assert [(s.kind, s.name, s.part) for s in model.activities[0].steps] == [
        ("fork", "", ""), ("action", "stream", "Camera"), ("action", "expose", "Camera"), ("join", "", ""),
        ("accept", "shutter", "Display"), ("action", "save", "Camera")]


def test_sequences_follow_calls_from_the_actors_across_parts_in_code_order(tmp_path):
    model = model_of(tmp_path, PHONE)
    (seq,) = model.sequences
    assert seq.name == "IncomingCalls"  # the file's docstring
    assert seq.lifelines == ["Network", "Processor", "Display", "User", "Speaker"]
    assert [(m.source, m.target, m.operation) for m in seq.messages] == [
        ("Network", "Processor", "wake"), ("Processor", "Display", "show_caller"),
        ("User", "Display", "answer"), ("Display", "Processor", "answered"), ("Processor", "Speaker", "connect")]


def test_a_status_set_from_a_service_takes_its_source_from_the_refusals_before_it(tmp_path):
    model = model_of(tmp_path, RENTAL)
    assert [(p.name, p.role, p.multiplicity) for p in model.parts] == [
        ("Rental", "", "1"), ("Bike", "bikes", "0..*"), ("Rider", "riders", "0..*")]
    assert transitions(model) == [
        ("initial", "FREE", "", ""),
        ("FREE", "HELD", "hold(rider_id, bike_id)", ""),
        # BROKEN and RIDING are refused; HELD only for the rider holding it
        ("FREE", "RIDING", "start(rider_id, bike_id)", "rider.balance >= MIN_BALANCE"),
        ("HELD", "RIDING", "start(rider_id, bike_id)", "rider.balance >= MIN_BALANCE and holder == rider_id"),
        ("any", "BROKEN", "report(bike_id, fault)", "fault"),
        ("any", "FREE", "finish(bike_id)", "")]  # set in a private helper: triggered by its caller
    assert {(v.owner, v.name, v.value) for v in model.values} >= {("Bike", "battery", "100"),
                                                                  ("Rental", "MIN_BALANCE", "3")}


def test_a_data_record_listing_typed_fields_is_not_taken_for_the_system(tmp_path):
    files = {"m.py": ("from typing import List\nfrom pydantic import BaseModel\n\n\nclass A:\n    pass\n\n\n"
                      "class B:\n    pass\n\n\nclass Report(BaseModel):\n    a: List[A] = []\n    b: List[B] = []\n")}
    assert model_of(tmp_path, files).parts == []


def test_ids_follow_the_code_so_the_same_code_gets_the_same_model(tmp_path):
    first = model_of(tmp_path / "a", PHONE).to_json()
    second = model_of(tmp_path / "b", PHONE).to_json()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert [p["id"] for p in first["parts"]] == ["BLK_01", "BLK_02", "BLK_03", "BLK_04", "BLK_05",
                                                 "ACTOR_01", "ACTOR_02"]


def test_a_fact_whose_names_are_not_on_its_lines_is_dropped_with_the_reason(tmp_path):
    repo = write(tmp_path, {"m.py": "LIMIT = 5\nclass Pump:\n    pass\n"})
    ev = lambda a, b: [Evidence(file="m.py", start_line=a, end_line=b)]  # noqa: E731
    model = SystemModel(
        parts=[Part(name="Pump", kind="system", evidence=ev(2, 3)), Part(name="Valve", evidence=ev(2, 3))],
        values=[Value(owner="Pump", name="LIMIT", value="5", evidence=ev(1, 1)),
                Value(owner="Pump", name="LIMIT", value="7", evidence=ev(1, 1))],
        states=[State(owner="Pump", name="LIMIT", evidence=ev(1, 1))],
        transitions=[Transition(owner="Pump", source="LIMIT", target="LIMIT", evidence=ev(40, 41))])
    check(model, FileCache(repo))
    assert [p.name for p in model.parts] == ["Pump"]
    assert [v.value for v in model.values] == ["5"]
    assert model.transitions == []
    assert {(r["kind"], r["reason"]) for r in model.rejected} == {
        ("part", "Valve not on the cited lines"), ("value", "7 not on the cited lines"),
        ("transition", "cites lines that are not in the file")}


def test_requirements_link_by_cited_lines_and_a_catch_all_states_nothing_on_its_own(tmp_path):
    rel = "phone/phone.py"
    off = line_of(PHONE, rel, "if self.state is State.OFF:")
    reqs = [req("POWER", rel, off, off + 1),
            req("ALL", rel, line_of(PHONE, rel, "def _enter"), line_of(PHONE, rel, "def tick") + 2),
            req("DEMO", "demo.py", 1, 3)]
    model = model_of(tmp_path, PHONE, reqs)
    own = {(lk.requirement, lk.element) for lk in model.links if lk.own}
    power = next(t.id for t in model.transitions if t.trigger == "power_pressed()")
    assert ("POWER", power) in own
    assert not any(r == "ALL" and e.startswith(("TR_", "ST_")) for r, e in own)
    cov = model.coverage
    assert cov["catch_all"] == ["ALL"] and cov["unmatched"] == ["DEMO"]
    unlock_tr = next(t.id for t in model.transitions if t.trigger.startswith("unlock"))
    assert unlock_tr in cov["unstated"] and power not in cov["unstated"]
    assert cov["stated"]["transition"] == [1, 4]  # the initial transition needs no requirement


def test_starting_values_and_states_with_no_entry_action_need_no_requirement(tmp_path):
    model = model_of(tmp_path, PHONE)
    link(model, [])
    unstated = set(model.coverage["unstated"])
    by_name = {v.name: v.id for v in model.values}
    assert by_name["IDLE_TIMEOUT"] in unstated and by_name["temp"] not in unstated
    states = {s.name: s.id for s in model.states}
    assert states["OFF"] in unstated and states["THROTTLED"] not in unstated


def test_markdown_section_shows_the_model_and_what_no_requirement_states(tmp_path):
    rel = "phone/phone.py"
    off = line_of(PHONE, rel, "if self.state is State.OFF:")
    text = "\n".join(markdown(model_of(tmp_path, PHONE, [req("POWER", rel, off, off + 1)])))
    assert "## Behaviour model (read from the code)" in text
    assert "| OFF | LOCKED | power_pressed() | POWER |" in text
    assert "### Sequence IncomingCalls" in text and "Network → Processor: wake" in text
    assert "No requirement of their own:" in text


def test_assign_ids_puts_the_system_first_and_actors_last():
    model = SystemModel(parts=[Part(name="User", kind="actor"), Part(name="B"), Part(name="Sys", kind="system")])
    assign_ids(model)
    assert [(p.name, p.id) for p in model.parts] == [("Sys", "BLK_01"), ("B", "BLK_02"), ("User", "ACTOR_01")]


class OneClaimLLM:
    """Answers every module prompt with one requirement citing the power-button transition."""

    def __init__(self, rel, start, end):
        self.answer = {"requirements": [{
            "text": "The phone shall lock when the power button is pressed while off.", "type": "functional",
            "evidence": [{"file": rel, "start_line": start, "end_line": end}]}]}

    def complete(self, system, user, json_mode=False):
        return json.dumps(self.answer)


def test_a_run_writes_model_json_beside_the_requirements_and_links_them(tmp_path):
    repo = write(tmp_path / "repo", PHONE)
    rel = "phone/phone.py"
    off = line_of(PHONE, rel, "if self.state is State.OFF:")
    report = run(repo, str(tmp_path / "out"), OneClaimLLM(rel, off, off + 1), "fake/model", workers=1,
                 profile="sysml")
    model = json.loads((tmp_path / "out" / "model.json").read_text(encoding="utf-8"))
    power = next(t for t in model["transitions"] if t["trigger"] == "power_pressed()")
    rid = report.requirements[0].id
    assert {"requirement": rid, "element": power["id"], "own": True} in model["links"]
    assert "## Behaviour model (read from the code)" in (tmp_path / "out" / "requirements.md").read_text(
        encoding="utf-8")
    assert "model" not in json.loads((tmp_path / "out" / "requirements.json").read_text(encoding="utf-8"))


# ---------- the model spec: what SysModeler generates the model from ----------

def phone_spec(tmp_path, reqs=()):
    from code2req.model.spec import build_spec
    model = model_of(tmp_path, PHONE, reqs)
    return build_spec(model, list(reqs), model.description)


def test_the_spec_lists_every_element_with_an_id_and_what_implements_each_requirement(tmp_path):
    rel = "phone/phone.py"
    off = line_of(PHONE, rel, "if self.state is State.OFF:")
    spec = phone_spec(tmp_path, [req("POWER", rel, off, off + 1), req("DEMO", "demo.py", 1, 3)])
    assert spec["diagrams"] == ["Requirements", "Structure", "Machine States", "Activity Capture",
                                "Sequence IncomingCalls"]
    # operations are numbered as the SysML text declares them, the phone's first; TR_00 is the
    # initial transition, so power_pressed's OFF -> LOCKED is TR_01
    assert [(r["id"], r["implemented_by"]) for r in spec["requirements"]] == [("POWER", "OP_02, TR_01")]
    assert spec["left_out"] == ["DEMO"]  # cites nothing in the model: the generator would model it
    assert [b["id"] + " " + b["name"] for b in spec["blocks"]] == [
        "BLK_01 PhoneDef", "BLK_02 DisplayDef", "BLK_03 ProcessorDef", "BLK_04 SpeakerDef", "BLK_05 CameraDef"]
    assert spec["blocks"][0]["values"][:2] == ["IDLE_TIMEOUT : DurationValue = 30.0 [s]",
                                               "THROTTLE_TEMP : Real = 45.0 (°C)"]
    assert not any(o["block"] in ("NetworkDef", "UserDef") for o in spec["operations"])  # actors are outside
    (machine,) = spec["machines"]
    assert machine["initial"] == "OFF" and [s["id"] for s in machine["states"]] == ["ST_01", "ST_02", "ST_03", "ST_04"]
    assert spec["count_check"].startswith("1 requirements · 1 satisfy · 5 blocks · 4 parts · 2 actors")


def test_activity_nodes_lead_to_the_next_a_fork_to_its_branches_and_branches_to_the_join(tmp_path):
    (activity,) = phone_spec(tmp_path)["activities"]
    assert [(n["id"], n["text"], n.get("next")) for n in activity["nodes"]] == [
        ("ACT_01", "initial", ["ACT_02"]), ("ACT_02", "fork", ["ACT_03", "ACT_04"]),
        ("ACT_03", "stream in Camera", ["ACT_05"]), ("ACT_04", "expose in Camera", ["ACT_05"]),
        ("ACT_05", "join", ["ACT_06"]), ("ACT_06", "accept shutter in Display", ["ACT_07"]),
        ("ACT_07", "save in Camera", ["ACT_08"]), ("ACT_08", "final", None)]


def test_a_state_set_without_checking_the_state_is_entered_from_every_other_state(tmp_path):
    from code2req.model.spec import build_spec
    model = model_of(tmp_path, RENTAL)
    spec = build_spec(model, [], model.description)
    rows = [(t["from"], t["to"], t["trigger"]) for t in spec["machines"][0]["transitions"]]
    assert [r for r in rows if r[2] == "report(bike_id, fault)"] == [
        ("FREE", "BROKEN", "report(bike_id, fault)"), ("HELD", "BROKEN", "report(bike_id, fault)"),
        ("RIDING", "BROKEN", "report(bike_id, fault)")]
    assert any("sets BROKEN without checking the state" in d for d in spec["decisions"])
    assert spec["diagrams"] == ["Requirements", "Structure", "Machine States"]  # no actor calls: no sequence


def test_the_same_code_gives_the_same_spec_word_for_word(tmp_path):
    from code2req.model.spec import markdown
    first, second = markdown(phone_spec(tmp_path / "a")), markdown(phone_spec(tmp_path / "b"))
    assert first == second
    assert "Model exactly the elements below" in first and "## 7. Do not add" in first


def test_write_spec_writes_markdown_and_pdf_and_never_reads_requirement_text_as_markup(tmp_path):
    from code2req.model.spec import write_spec
    rel = "phone/phone.py"
    off = line_of(PHONE, rel, "if self.state is State.OFF:")
    hostile = req("POWER", rel, off, off + 1)
    hostile.text = r'#panic("markup was evaluated") @nowhere <x> $x^2$ *bold* ] [ \ ` unbalanced; done.'
    model = model_of(tmp_path / "repo", PHONE, [hostile])
    out = tmp_path / "out"
    out.mkdir()
    (out / "model_spec.pdf").write_bytes(b"an earlier run's spec")
    write_spec(str(out), model, [hostile])
    assert (out / "model_spec.pdf").read_bytes().startswith(b"%PDF")
    assert "#panic" in (out / "model_spec.md").read_text(encoding="utf-8")
    assert write_spec(str(out), SystemModel(), []) is None
    assert not (out / "model_spec.md").exists() and not (out / "model_spec.pdf").exists()


def test_spec_command_writes_the_spec_for_a_finished_run(tmp_path, capsys):
    from code2req.cli import main
    repo = write(tmp_path / "repo", PHONE)
    rel = "phone/phone.py"
    off = line_of(PHONE, rel, "if self.state is State.OFF:")
    run(repo, str(tmp_path / "out"), OneClaimLLM(rel, off, off + 1), "fake/model", workers=1, profile="sysml")
    assert (tmp_path / "out" / "model_spec.pdf").is_file()
    (tmp_path / "out" / "model_spec.md").unlink()
    assert main(["spec", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "model_spec.md").is_file()
    assert (tmp_path / "out" / "sysml" / "model" / "system" / "system.sysml").is_file()
    out = capsys.readouterr().out
    assert "model_spec.md" in out and "SysML file(s)" in out


# ---------- the SysML text ----------

# The ATM code written from the deterministic ATM spec, whose exact SysML text SysModeler copied
# the same way in every run: the text written from the code must say what that text says.
ATM_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "atm")


def atm_files():
    pkg = os.path.join(ATM_DIR, "atm")
    out = {}
    for name in sorted(os.listdir(pkg)):
        if name.endswith(".py"):
            with open(os.path.join(pkg, name), encoding="utf-8") as f:
                out[f"atm/{name}"] = f.read()
    return out


def sysml_of(tmp_path, files, reqs=()):
    from code2req.model.sysml import build_sysml
    return build_sysml(model_of(tmp_path, files, reqs), list(reqs))


def block(text, head):
    """The lines from the one that is head to the end of its statement, indents removed: the
    brace head opens closing, or else the first semicolon outside braces."""
    lines = [x.strip() for x in text.splitlines()]
    start = lines.index(head)
    depth = 0
    for end in range(start, len(lines)):
        depth += lines[end].count("{") - lines[end].count("}")
        if depth == 0 and lines[end].endswith("}" if head.endswith("{") else ";"):
            return lines[start:end + 1]
    return lines[start:]


def test_the_atm_gives_the_transitions_of_the_deterministic_spec_id_by_id(tmp_path):
    sysml = sysml_of(tmp_path, atm_files())
    text = sysml.files["model/system/system.sysml"]
    heads = [x.strip() for x in text.splitlines() if x.strip().startswith("transition <'")]
    rows = [(head.split("'")[1], b[1].removeprefix("first "), b[-1].removeprefix("then ").rstrip(";"))
            for head in heads for b in [block(text, head)]]
    # the deterministic spec's TR_01-TR_17: state by state, call events, then when, then after
    assert rows == [
        ("TR_01", "IDLE", "AWAITING_PIN"), ("TR_02", "IDLE", "OUT_OF_SERVICE"),
        ("TR_03", "AWAITING_PIN", "VERIFYING_PIN"), ("TR_04", "AWAITING_PIN", "IDLE"), ("TR_05", "AWAITING_PIN", "IDLE"),
        ("TR_06", "VERIFYING_PIN", "MENU"), ("TR_07", "VERIFYING_PIN", "AWAITING_PIN"), ("TR_08", "VERIFYING_PIN", "IDLE"),
        ("TR_09", "MENU", "AUTHORIZING"), ("TR_10", "MENU", "MENU"), ("TR_11", "MENU", "AWAITING_BALANCE"),
        ("TR_12", "MENU", "IDLE"), ("TR_13", "MENU", "IDLE"), ("TR_14", "AUTHORIZING", "IDLE"),
        ("TR_15", "AUTHORIZING", "MENU"), ("TR_16", "AWAITING_BALANCE", "MENU"), ("TR_17", "OUT_OF_SERVICE", "IDLE")]
    lines = [x.strip() for x in text.splitlines()]
    at = lines.index("comment <'TR_00'> /* initial to IDLE */")
    assert lines[at + 1:at + 3] == ["entry;", "then IDLE;"]
    assert block(text, "transition <'TR_08'> tr_08") == [
        "transition <'TR_08'> tr_08", "first VERIFYING_PIN", "accept PinResult",
        # the event's data is read from the operation the machine performs, which SysModeler's
        # parser resolves (it leaves ev.valid of `accept ev : PinResult` empty)
        "if not machine.pin_result.valid and machine.pin_attempts + 1 >= machine.MAX_PIN_ATTEMPTS",
        "do action on_pin_result {",
        "action retain_card {", "perform machine.card_reader.retain_card;", "}",
        "then action set_pin_attempts {", "assign machine.pin_attempts := 0;", "}",
        "then action set_current_card {", 'assign machine.current_card := "";', "}",
        "}", "then IDLE;"]
    assert block(text, "transition <'TR_02'> tr_02")[2:4] == [
        "accept when machine.cash_level < machine.LOW_CASH_LEVEL", "do action report_low_cash {"]
    assert block(text, "transition <'TR_05'> tr_05")[2] == "accept after machine.SESSION_TIMEOUT"
    assert block(text, "transition <'TR_09'> tr_09")[3] == "if machine.select_withdrawal.amount <= machine.cash_level"
    assert sysml.notes == []  # nothing the code does is left out


def test_an_operation_that_is_only_steps_is_an_action_def_its_transitions_perform(tmp_path):
    text = sysml_of(tmp_path, atm_files()).files["model/system/system.sysml"]
    assert block(text, "action def <'OP_10'> DispenseCash {") == [
        "action def <'OP_10'> DispenseCash {", "in ref part owner : AtmDef;",
        "action dispense_notes {", "perform owner.cash_dispenser.dispense_notes {",
        "in amount = owner.requested_amount;", "}", "}",
        "then action set_cash_level {", "assign owner.cash_level := owner.cash_level - owner.requested_amount;", "}",
        "then action print_receipt {", "perform owner.receipt_printer.print_receipt {",
        "in amount = owner.requested_amount;", "}", "}",
        "then action eject_card {", "perform owner.card_reader.eject_card;", "}",
        "then action set_current_card {", 'assign owner.current_card := "";', "}",
        "then action set_requested_amount {", "assign owner.requested_amount := 0;", "}",
        "then done;", "}"]
    assert block(text, "transition <'TR_14'> tr_14")[3:8] == [
        "if machine.withdrawal_result.approved", "do action dispense_cash {", "perform machine.dispense_cash {",
        "in owner = machine;", "}"]
    # an event handler's steps are its transitions' effects, not a body of its own
    assert block(text, "action def <'OP_01'> InsertCard {") == [
        "action def <'OP_01'> InsertCard {", "in attribute card_number : String;", "}"]


def test_each_part_is_a_package_and_a_part_the_system_is_handed_is_a_reference(tmp_path):
    files = sysml_of(tmp_path, atm_files()).files
    assert list(files) == ["model/system/types.sysml", "model/system/card_reader/card_reader.sysml",
                           "model/system/cash_dispenser/cash_dispenser.sysml",
                           "model/system/receipt_printer/receipt_printer.sysml", "model/system/display/display.sysml",
                           "model/system/bank_network/bank_network.sysml", "model/system/system.sysml"]
    assert "attribute def Money :> Integer;" in files["model/system/types.sysml"]
    assert [x.strip() for x in files["model/system/card_reader/card_reader.sysml"].splitlines()] == [
        "package CardReader {", "private import ScalarValues::*;", "action def <'OP_12'> EjectCard;",
        "action def <'OP_13'> RetainCard;", "part def <'BLK_02'> CardReaderDef {", "action eject_card : EjectCard;",
        "action retain_card : RetainCard;", "perform eject_card;", "perform retain_card;", "}", "}"]
    atm = files["model/system/system.sysml"]
    assert "ref part <'REL_05'> bank_network : BankNetwork::BankNetworkDef[1];" in atm
    assert "attribute SESSION_TIMEOUT : DurationValue = 30.0 [s];" in atm
    assert 'attribute current_card : String = "";' in atm and "attribute cash_level : Types::Money = 200000;" in atm


def test_requirements_that_cite_an_element_are_defined_and_satisfied_by_the_system(tmp_path):
    rel = "phone/phone.py"
    off = line_of(PHONE, rel, "if self.state is State.OFF:")
    files = sysml_of(tmp_path, PHONE, [req("POWER", rel, off, off + 1), req("DEMO", "demo.py", 1, 3)]).files
    assert [x.strip() for x in files["model/requirements/system_requirements.sysml"].splitlines()[1:5]] == [
        "requirement def <'POWER'> POWER {", "doc /* The phone shall POWER. */", "subject system : Phone::PhoneDef;", "}"]
    phone = files["model/system/system.sysml"]
    assert "requirement power : SystemRequirements::POWER;" in phone and "satisfy power by phone;" in phone
    assert "DEMO" not in "".join(files.values())  # cites nothing in the model


def test_a_part_reaching_the_system_through_a_field_holds_it_by_reference(tmp_path):
    processor = sysml_of(tmp_path, PHONE).files["model/system/processor/processor.sysml"]
    assert "ref part phone : Phone::PhoneDef[1];" in processor
    # the call is written inside Wake, whose owner the called action's own owner would hide
    assert block(processor, "action def <'OP_12'> Wake {")[2:6] == [
        "action show_caller {", "perform Wake::owner.phone.display.show_caller {",
        "in owner = Wake::owner.phone.display;", "}"]
    assert "perform owner.phone.speaker.'connect';" in processor  # a keyword is quoted


def test_states_have_their_entry_actions_and_an_activity_is_its_function_s_steps(tmp_path):
    phone = sysml_of(tmp_path, PHONE).files["model/system/system.sysml"]
    assert block(phone, "state <'ST_01'> OFF {") == [
        "state <'ST_01'> OFF {", "entry action turn_off {", "perform machine.display.turn_off;", "}", "}"]
    assert block(phone, "action def <'OP_01'> Capture {")[3:9] == [
        "first start;", "then fork fork_1;", "then stream;", "then 'expose';", "action stream {",
        "perform owner.camera.stream;"]
    assert "first stream then join_1;" in phone and "join join_1;" in phone
    assert phone.index("join join_1;") < phone.index("then action accept_shutter {") < phone.index("then action save {")


def test_what_sysml_cannot_say_is_left_out_with_a_note_and_never_guessed(tmp_path):
    sysml = sysml_of(tmp_path, RENTAL)
    bike = sysml.files["model/system/bike/bike.sysml"]
    note = "// left out: guard condition rider.balance >= MIN_BALANCE (rider is not a field, part or parameter of Bike)"
    assert bike.count(note) == 2
    assert block(bike, "transition <'TR_02'> tr_02")[2:4] == ["accept Rental::Start", "then RIDING;"]
    # the conditions it can say are kept; the bike cannot reach the rental, so the event's data
    # is named by its def
    assert block(bike, "transition <'TR_04'> tr_04")[3] == "if machine.holder == Rental::Start::rider_id"
    assert sysml.notes[0].startswith("TR_02 FREE -> RIDING: guard condition `rider.balance >= MIN_BALANCE` left out")


def test_a_module_function_named_like_a_method_takes_its_file_s_name(tmp_path):
    files = dict(PHONE)
    files["phone/phone.py"] = files["phone/phone.py"].replace(
        "    def tick(self):", "    def capture(self):\n        pass\n\n    def tick(self):")
    phone = sysml_of(tmp_path, files).files["model/system/system.sysml"]
    assert "action def <'OP_01'> CameraCapture {" in phone  # camera.capture(phone), with its fork
    assert re.search(r"action def <'OP_\d+'> Capture;", phone)  # Phone.capture keeps the name
    assert "action camera_capture : CameraCapture;" in phone and "action capture : Capture;" in phone


def test_expressions_are_written_with_sysml_precedence():
    from code2req.model.sysml import _Scope, _Writer
    model = SystemModel(parts=[Part(name="A", kind="system")],
                        values=[Value(owner="A", name="x", value="1"), Value(owner="A", name="LIMIT", value="3")])
    w, scope = _Writer(model, []), _Scope("machine", "A", {"n": "ev.n"})
    assert w.expr("not x > 1", scope) == "not (machine.x > 1)"  # SysML's not binds tighter than >
    assert w.expr("0 < n <= LIMIT", scope) == "0 < ev.n and ev.n <= machine.LIMIT"
    assert w.expr("(x + 1) * n - 2", scope) == "(machine.x + 1) * ev.n - 2"
    assert w.expr('f"left {x} of {LIMIT}"', scope) == '"left " + machine.x + " of " + machine.LIMIT'
    assert w.expr("x is None or n != True", scope) == "machine.x == null or ev.n != true"


def test_the_spec_numbers_elements_as_the_sysml_text_and_ends_with_it(tmp_path):
    from code2req.model.spec import build_spec, markdown
    from code2req.model.sysml import build_sysml
    model = model_of(tmp_path, atm_files())
    sysml = build_sysml(model, [])
    spec = build_spec(model, [], model.description, sysml)
    text = sysml.text()
    rows = spec["machines"][0]["transitions"]
    assert [t["id"] for t in rows] == [f"TR_{i:02d}" for i in range(18)]
    assert all(f"<'{t['id']}'>" in text for t in rows)
    assert len(spec["operations"]) == 21 and all(f"<'{o['id']}'> " in text for o in spec["operations"])
    assert rows[1]["effect"] == "current_card := card_number; pin_attempts := 0"
    md = markdown(spec)
    assert md.count("```sysml") == 7 and "## 7. Exact SysML v2 text" in md
    assert "Generate exactly the SysML v2 text in section 7, file by file, and nothing else." in md


def test_the_same_code_gives_the_same_sysml_character_for_character(tmp_path):
    first, second = sysml_of(tmp_path / "a", PHONE), sysml_of(tmp_path / "b", PHONE)
    assert first.files == second.files


def test_a_script_beside_the_package_adds_no_operation_to_the_system(tmp_path):
    files = {**PHONE, "demo.py": "from phone.phone import Phone\n\n\ndef step(label, action):\n    print(label)\n"}
    model = model_of(tmp_path, files)
    assert "step" not in {o.name for o in model.operations}
    assert "capture" in {o.name for o in model.operations if o.owner == "Phone"}  # phone/camera.py is the phone's
