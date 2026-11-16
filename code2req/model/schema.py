"""The behaviour model: the parts a codebase builds, its values, states, transitions,
activities and the calls between its parts, each citing the code lines it comes from.

Citations are the same Evidence a requirement carries, so requirements and model elements
are linked by line overlap whatever language the code is in."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from code2req.schema import Evidence

PartKind = Literal["system", "part", "actor"]
StepKind = Literal["action", "accept", "fork", "join"]


class Part(BaseModel):
    id: str = ""
    name: str  # the class or type: Display
    kind: PartKind = "part"
    role: str = ""  # what its parent holds it by: display ("" for the system and actors)
    parent: str = ""  # the parent part's name; "" for the system and actors
    multiplicity: str = "1"
    reference: bool = False  # handed to its parent, not built by it: the bank network an ATM is given
    evidence: list[Evidence] = Field(default_factory=list)


class Value(BaseModel):
    id: str = ""
    owner: str  # part name
    name: str
    value: str = ""  # as written in the code: 30.0, Decimal("3.00") -> 3.00
    unit: str = ""
    type: str = ""  # the type the code declares, as written: int, Money; "" when it declares none
    evidence: list[Evidence] = Field(default_factory=list)


class ValueType(BaseModel):
    """A named type for a primitive one (Money = int), declared as the code declares it."""

    name: str
    base: str  # as written: int
    evidence: list[Evidence] = Field(default_factory=list)


class Operation(BaseModel):
    id: str = ""
    owner: str
    name: str
    params: list[str] = Field(default_factory=list)
    types: list[str] = Field(default_factory=list)  # each parameter's declared type, "" when none
    # What it does, written as transition effects are, when its code is only such steps (no
    # branch or loop) and no transition already says it: [] otherwise
    body: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)


class State(BaseModel):
    id: str = ""
    owner: str  # the part whose state it is
    name: str
    entry: list[str] = Field(default_factory=list)  # Part.operation calls made on entering it
    evidence: list[Evidence] = Field(default_factory=list)


class Transition(BaseModel):
    id: str = ""
    owner: str
    source: str  # a state, "initial", or "any" when the code sets the state without checking it
    target: str
    trigger: str = ""  # power_pressed(), after(IDLE_TIMEOUT), when(internal_temp > THROTTLE_TEMP)
    guard: str = ""
    # What the code does on the way, in order: field := value, part.operation(args), operation(args)
    effect: list[str] = Field(default_factory=list)
    internal: bool = False  # handles the event without leaving the state (no exit, no entry)
    evidence: list[Evidence] = Field(default_factory=list)


class Step(BaseModel):
    kind: StepKind = "action"
    name: str = ""
    part: str = ""  # the part that runs it
    evidence: list[Evidence] = Field(default_factory=list)


class Activity(BaseModel):
    id: str = ""
    owner: str
    name: str
    steps: list[Step] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)


class Call(BaseModel):
    """One call from an operation of one part to an operation of another."""

    source: str
    source_operation: str
    target: str
    operation: str
    evidence: list[Evidence] = Field(default_factory=list)


class Message(BaseModel):
    source: str
    target: str
    operation: str
    evidence: list[Evidence] = Field(default_factory=list)


class Sequence(BaseModel):
    id: str = ""
    name: str
    lifelines: list[str] = Field(default_factory=list)
    messages: list[Message] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)


class Link(BaseModel):
    """A requirement that cites an element's lines. own is False when the requirement cites
    so many elements of that kind at once that it states none of them on its own."""

    requirement: str
    element: str
    own: bool = True


class SystemModel(BaseModel):
    source: str = ""  # what read the facts from the code: "python"
    description: str = ""  # what the system is, in the code's own words (its module docstring)
    parts: list[Part] = Field(default_factory=list)
    types: list[ValueType] = Field(default_factory=list)  # the named types the values and parameters use
    values: list[Value] = Field(default_factory=list)
    operations: list[Operation] = Field(default_factory=list)
    states: list[State] = Field(default_factory=list)
    transitions: list[Transition] = Field(default_factory=list)
    activities: list[Activity] = Field(default_factory=list)
    calls: list[Call] = Field(default_factory=list)
    sequences: list[Sequence] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    coverage: dict = Field(default_factory=dict)
    rejected: list[dict] = Field(default_factory=list)  # facts the checks dropped, with the reason

    def elements(self) -> list[tuple[str, BaseModel]]:
        """(kind, element) for everything that gets an ID, in ID order."""
        kinds = (("part", self.parts), ("value", self.values), ("operation", self.operations),
                 ("state", self.states), ("transition", self.transitions),
                 ("activity", self.activities), ("sequence", self.sequences))
        return [(("actor" if kind == "part" and e.kind == "actor" else kind), e) for kind, items in kinds for e in items]

    def to_json(self) -> dict:
        """Citations as file and lines only: the verifier's fields mean nothing here."""
        def trim(node):
            if isinstance(node, dict):
                if node.keys() >= {"file", "start_line", "end_line"}:
                    return {k: node[k] for k in ("file", "start_line", "end_line")}
                return {k: trim(v) for k, v in node.items()}
            return [trim(v) for v in node] if isinstance(node, list) else node
        return trim(self.model_dump(exclude_none=True))
