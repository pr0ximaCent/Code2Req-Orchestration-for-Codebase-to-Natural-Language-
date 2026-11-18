"""Names in the forms SysModeler writes them: PascalCase defs, lowerCamel usages, snake_case files."""
import re

# SysML v2 keywords: a usage named like one would not parse, so it takes a suffix.
KEYWORDS = set("""about abstract accept action actor after alias all allocate allocation analysis and as assert assign
assoc assume at attribute behavior bind binding bool by calc case chains class classifier comment composite concern
conjugate conjugation connect connection connector const constant constraint crosses datatype decide def default
dependency derived differences disjoining disjoint do doc else end entry enum event exhibit exit expose expr false
feature featured featuring filter first flow for fork frame from function hastype if implies import in include
individual inout interaction interface intersects inv inverse inverting istype item join language library locale
loop member merge message meta metaclass metadata multiplicity namespace new nonunique not null objective occurrence
of or ordered out package parallel part perform port portion predicate private protected public redefines
redefinition ref references render rendering rep require requirement return satisfy send snapshot specialization
specializes stakeholder standard state step struct subclassifier subject subset subsets subtype succession
terminate then timeslice to transition true type typed typing unions until use var variant variation verification
verify via view viewpoint when while xor done start""".split())


def words(name: str) -> list[str]:
    """CardReader, card_reader, ATMState -> ['card', 'reader'], ['atm', 'state']."""
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    return [w.lower() for w in re.split(r"[^0-9A-Za-z]+", name) if w]


def pascal(name: str) -> str:
    return "".join(w[:1].upper() + w[1:] for w in words(name))


def camel(name: str) -> str:
    p = pascal(name)
    out = p[:1].lower() + p[1:]
    return out + "Value" if out in KEYWORDS else out


def snake(name: str) -> str:
    return "_".join(words(name))


def is_keyword(name: str) -> bool:
    return (name[:1].lower() + name[1:]) in KEYWORDS
