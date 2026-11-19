"""The design reader's checked answers as the design schema golang.py writes (so render.py, sysml.py and
the requirement anchoring take either): IDs, parts, subsystems, external systems, connections from the
checked call steps, routes, operations, activities in the step form the document and the SysML writer
read, interactions stitched from the calls down to the external systems, state machines from the
status changes, settings, limits, data and the code spans requirements are anchored by."""
from __future__ import annotations

import os
import re
from collections import Counter, OrderedDict

from .mounts import Mounts
from .names import camel, pascal, words
from .reader import HTTP_METHODS as HTTP
from .reader import SKIP, SYSTEM, norm_actor

# who calls the other ways in: (the map's kinds, the actor, what it does)
ENTRY_ACTORS = [({"RPC", "GRPC"}, "RpcClient", "calls the system's RPC methods"),
                ({"QUERY", "MUTATION", "SUBSCRIPTION", "GRAPHQL"}, "GraphQlClient", "sends the system's GraphQL queries and mutations"),
                ({"CLI", "COMMAND"}, "Operator", "runs the system's commands"),
                ({"CONSUMER", "EVENT", "MESSAGE", "QUEUE", "TOPIC", "SUBSCRIBER"}, "MessageBroker", "delivers the messages the system consumes"),
                ({"SCHEDULED", "CRON", "JOB"}, "Scheduler", "runs the system's scheduled jobs")]
# the annotation or decorator of a way in, by its kind
ENTRY_MARKS = [
    # not Spring Data's @Query("select ..."), a repository's SQL
    ("QUERY", re.compile(r"@(Dgs(Query|Data)|QueryMapping|SchemaMapping|ResolveField|FieldResolver)\b|@Query\b(?!\s*\(\s*(value\s*=\s*)?[\"'])"
                         r"|@strawberry\.field|@query\.field")),
    ("MUTATION", re.compile(r"@(DgsMutation|MutationMapping|Mutation)\b|@strawberry\.mutation|@mutation\.field")),
    ("SUBSCRIPTION", re.compile(r"@(DgsSubscription|SubscriptionMapping|Subscription)\b")),
    ("RPC", re.compile(r"@(GrpcMethod|GrpcStreamMethod|MessagePattern|RpcMethod)\b")),
    ("CONSUMER", re.compile(r"@(KafkaListener|RabbitListener|JmsListener|SqsListener|StreamListener|EventListener|EventPattern|OnEvent"
                            r"|Subscribe|Consumer|Process|Processor|Listener|TransactionalEventListener)\b|@\w+\.task\b|@shared_task\b")),
    ("SCHEDULED", re.compile(r"@(Scheduled|Cron|Interval|Timeout|Schedule)\b|@\w+\.(scheduled_job|periodic_task|on_after_configure)\b")),
    ("CLI", re.compile(r"@(\w+\.)?(command|Command|ShellMethod|subcommand)\b")),
]


def annotated_kind(r, fn) -> str:
    """The kind of way in the annotations or decorators on fn name (@DgsMutation: MUTATION), or ''."""
    if fn is None:
        return ""
    text = r.marks_at(fn.file, fn.line)
    kind = next((kind for kind, rx in ENTRY_MARKS if rx.search(text)), "")
    if kind == "QUERY":  # a resolver of a field of the Mutation or Subscription type: @DgsData(parentType = MUTATION...)
        kind = next((k for k in ("MUTATION", "SUBSCRIPTION") if re.search(rf"(?i)\b{k}\b", text)), kind)
    return kind

LANG_NAME = {"go": "Go", "python": "Python", "typescript": "TypeScript", "tsx": "TypeScript", "javascript": "JavaScript",
             "java": "Java", "csharp": "C#"}


# folders that say how code is organised, not what it is (the Go reader's list, and the other languages' roots)
SUB_NOISE = {"domains", "domain", "internal", "pkg", "services", "service", "handlers", "handler", "v1", "v2", "v3", "src", "lib",
             "libs", "modules", "module", "impl", "app", "apps", "cmd", "source", "java", "kotlin", "com", "org", "net", "io",
             "packages", "main", "controllers", "controller", "routes", "models"}


PLUMBING = {"Write", "WriteHeader", "Header", "Flush", "Hijack", "ReadFrom", "String", "Error", "Unwrap", "Len", "Less", "Swap",
            "ServeHTTP", "Format", "Debug", "Info", "Warn", "Warning", "Fatal", "Print", "Printf", "Println", "Log", "Debugf",
            "Infof", "Warnf", "Errorf", "Fatalf", "MarshalJSON", "UnmarshalJSON", "__str__", "__repr__", "toString", "toJSON"}


def subsystem_of(rel: str, depth: int = 1) -> str:
    """The folder a file sits in that names something (not src/, internal/, services/...)."""
    parts = [p for p in os.path.dirname(rel).replace("\\", "/").split("/") if p and p.lower() not in SUB_NOISE]
    return pascal(parts[min(depth, len(parts)) - 1]) if parts else ""


def args_of(s: dict) -> str:
    a = str(s.get("args", "") or "").strip()
    return a[:80] + ("…" if len(a) > 80 else "")


def assemble(r) -> dict:
    title = r.title
    pkg = pascal(title) or "System"
    langs = Counter(f.lang for f in r.outline.files.values())
    d = {"title": title, "package": pkg, "language": LANG_NAME.get(langs.most_common(1)[0][0], "") if langs else "",
         "module": os.path.basename(os.path.abspath(r.repo)), "mount": "", "reader": "model"}
    sys_name = pkg if pkg not in {pascal(n) for n in r.comps} else pkg + "System"  # an Atm class in system "Atm"
    d["system"] = {"id": "BLK_01", "name": title, "def": sys_name + "Def", "part": camel(sys_name),
                   "wiring": sorted({p["built"] for p in r.parts.values() if p["built"]})[:6]}

    # subsystems: the first folder that names something; one level deeper when it names them all alike
    subs = {n: subsystem_of(c.get("file", "")) for n, c in r.comps.items()}
    if len(set(subs.values())) <= 1 and len(r.comps) > 6:
        subs = {n: subsystem_of(c.get("file", ""), 2) for n, c in r.comps.items()}
    if len(set(subs.values())) <= 1:
        subs = {n: "" for n in r.comps}

    # routes: what the surfaces found, handled by what the map says
    routes, seen, used = [], set(), set()
    map_routes = [x for x in r.map_routes if isinstance(x, dict)]
    for s in r.report.get("surfaces", []):
        if s.get("kind") != "http_route":
            continue
        method, _, path = s["name"].partition(" ")
        # the model's route for this one: same method, the nearest line of the same file, else a path ending alike
        same = [m for m in map_routes if str(m.get("method", "")).upper() == method]
        near = [m for m in same if m.get("file") == s["file"] and abs(int(m.get("line") or 0) - s["line"]) <= 2]
        hit = (min(near, key=lambda m: abs(int(m.get("line") or 0) - s["line"])) if near else None) or next(
            (m for m in same if path.rstrip("/") and str(m.get("path", "")).rstrip("/").endswith(path.rstrip("/"))), None)
        full = str(hit.get("path")) if hit and len(str(hit.get("path", ""))) > len(path) and str(hit.get("path")).rstrip("/").endswith(
            path.rstrip("/")) else path
        key = (method, s["file"], s["line"])  # one registration; "/" in users/ and "/" in articles/ are two routes
        if key in seen:
            continue
        seen.add(key)
        if hit is not None:
            used.add(id(hit))
        routes.append({"method": method, "path": full, "comp": hit.get("component") if hit else None,
                       "op": hit.get("operation") if hit else None, "file": s["file"], "line": s["line"]})
    for m in map_routes:  # routes the surfaces did not find (another framework, a decorator)
        method, path = str(m.get("method", "")).upper(), str(m.get("path", ""))
        if id(m) in used or any(rt["method"] == method and rt["file"] == m.get("file") and abs(int(rt["line"] or 0) - int(m.get("line") or 0)) <= 2
                                for rt in routes):
            continue
        if method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):  # "tags", "": a path as written
            routes.append({"method": method, "path": "/" + path.lstrip("/"), "comp": m.get("component"), "op": m.get("operation"),
                           "file": m.get("file"), "line": m.get("line")})
    # mount prefixes: one handed to a registering function (UsersRegister(v1.Group("/users"))) applies to the routes
    # that function registers; a single one with no registrar (StripPrefix, app.use("/api", ...)) to every route
    def join(pre: str, path: str) -> str:
        return path if path.startswith(pre + "/") or path == pre else pre + ("" if path.startswith("/") else "/") + path

    by_registrar = {str(m.get("registrar") or "").split(".")[-1].split("(")[0]: str(m["prefix"]).rstrip("/")
                    for m in r.mounts if m.get("registrar") and str(m.get("prefix", "")).rstrip("/")}
    done = set()
    tree = Mounts(r.outline)  # nested routers (include_router, register_blueprint, use): the prefixes the code gives
    for k, rt in enumerate(routes):
        full = tree.full_path(rt.get("file") or "", int(rt.get("line") or 0)) if rt.get("file") else None
        if full:
            rt["path"] = full
            done.add(k)
            continue
        fn = r.outline.fn_at(rt.get("file") or "", int(rt.get("line") or 0)) if rt.get("file") else None
        if fn is not None and fn.name in by_registrar:
            rt["path"] = join(by_registrar[fn.name], rt["path"])
            done.add(k)
    prefixes = {str(m["prefix"]).rstrip("/") for m in r.mounts if not m.get("registrar") and str(m.get("prefix", "")).rstrip("/")}
    if not prefixes and not done:  # one mount whose registrar is a router object (app.use("/api", routes)): every route
        prefixes = {str(m["prefix"]).rstrip("/") for m in r.mounts if str(m.get("prefix", "")).rstrip("/")}
    if len(prefixes) == 1:
        pre = prefixes.pop()
        mount = next(m for m in r.mounts if str(m.get("prefix", "")).rstrip("/") == pre)
        for k, rt in enumerate(routes):
            if k not in done and not r.beside_mount(rt, mount):
                rt["path"] = join(pre, rt["path"])
    routes = [rt for rt in routes if rt["method"] in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")]
    merged = OrderedDict()
    for rt in routes:  # the same route found twice (by the surfaces and by the map) is one; /x/ is /x
        rt["path"] = rt["path"].rstrip("/") or "/"
        kept = merged.setdefault((rt["method"], rt["path"]), rt)
        if kept is not rt and not kept.get("comp") and rt.get("comp"):  # the copy that names the handler wins
            kept.update(comp=rt["comp"], op=rt["op"])
    routes = list(merged.values())
    for rt in routes:  # the handler must be a component's operation
        if rt["comp"] not in r.comps:
            rt["comp"] = next((n for n, c in r.comps.items() if rt["op"] in c["operations"]), None)

    # the router: every request reaches its handler through it. A function that builds a mux is no class the
    # map names, so a system with routes and no router component gets one, as the Go reader draws it.
    handlers = [rt["comp"] for rt in routes if rt["comp"]]
    # a router serves no route itself: it passes each on (a controller that registers its own routes is a handler)
    router_name = next((n for n, c in r.comps.items() if c["kind"] == "router" and n not in handlers), None)
    synthetic = None
    if router_name is None and handlers:
        router_name = "HttpRouter"
        while router_name in r.comps:
            router_name += "2"
        where = next(iter(r.mounts), None) or next((rt for rt in routes if rt.get("file")), {})
        r.comps[router_name] = {"name": router_name, "code": "", "file": where.get("file", ""), "lines": [where.get("line") or 0] * 2,
                                "kind": "router", "purpose": "receives every HTTP request, runs it through the middleware and "
                                "passes it to the handler of its route", "given": [], "implements": [], "external": [],
                                "operations": OrderedDict([("HandleRequest", {"name": "HandleRequest", "file": where.get("file", ""),
                                                                              "lines": [where.get("line") or 0] * 2})])}
        part = "httpRouter"
        while part in r.parts:
            part += "2"
        r.parts[part] = {"comp": router_name, "over": None, "passed": {}, "built": f"{where.get('file')}:{where.get('line')}"}
        subs[router_name] = Counter(subs[h] for h in handlers).most_common(1)[0][0]
        synthetic = router_name

    # ids
    order = sorted(r.comps, key=lambda n: (subs[n] or "~", n))
    ids = {n: f"BLK_{i:02d}" for i, n in enumerate(order, 2)}
    parts_of = {n: [k for k, p in r.parts.items() if p["comp"] == n] for n in r.comps}
    defs = {n: (pascal(n) + "Def") for n in r.comps}
    router = parts_of[router_name][0] if router_name and parts_of.get(router_name) else None

    # actors
    actors = OrderedDict()
    if routes:
        actors["ApiClient"] = {"name": "ApiClient", "what": "calls the system's HTTP routes", "clients": set()}
    actors.update(r.actors)
    actor_part = {a: camel(a) for a in actors}

    # steps of each operation in the document's form, and the calls they make
    pair_calls: dict[tuple[str, str], OrderedDict] = OrderedDict()
    op_steps: dict[tuple[str, str], list] = {}
    op_calls: dict[tuple[str, str], list] = {}  # (comp, op) -> [(target part or ext:, op, args)] in order
    counts: dict[tuple[str, str], Counter] = {}
    route_of = {(rt["comp"], rt["op"]): rt for rt in routes if rt["comp"]}

    def info(comp: str) -> dict:
        return r.system if comp == SYSTEM else r.comps.get(comp) or r.system

    def target_of(comp: str, pk: str, s: dict):
        call, to = str(s.get("call", "")), str(s.get("to", "") or "")
        op = str(s.get("op") or call.split(".")[-1]).split("(")[0]
        segs = [x for x in call.split(".") if x]
        field_ = segs[-2] if len(segs) >= 2 else ""
        tgt = r.field_target.get((pk, field_))
        if tgt is None and field_:
            tgt = next((v for (p, f), v in r.field_target.items() if p == pk and words(f) == words(field_)), None)
        own = info(comp)["operations"]
        if tgt is None and to:
            if to in ("SELF", comp, info(comp).get("code")) or (op in own and field_ in ("", "this", "self", "s")):
                return "self", op
            hit = r.resolve(to, info(comp).get("file", ""))
            if hit in r.comps and parts_of[hit]:
                tgt = parts_of[hit][0] if len(parts_of[hit]) == 1 else next(
                    (k for k in parts_of[hit] if field_ and field_.lower()[:5] in k.lower()), parts_of[hit][0])
            elif norm_actor(to) in actors:
                tgt = "ext:" + norm_actor(to)
        if tgt is None and op in own:
            return "self", op
        return tgt, op

    def convert(comp: str, pk: str, opname: str, steps: list, c: Counter, top: bool = False) -> list:
        steps = [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []
        if any(not minor(s) for s in steps):  # a value set, a log line or a loop of them is a step only where it is
            steps = [s for s in steps if not minor(s)]  # all its branch does (the Go reader's rule)
        out = []
        for s in steps:
            if not isinstance(s, dict):
                continue
            if "call" in s:
                tgt, op = target_of(comp, pk, s)
                a = args_of(s)
                if tgt == "self":
                    out.append({"do": f"Run {op}({a})" + (" — an operation of its own (section 6)." if op in info(comp)["operations"] else ".")})
                    op_calls[(comp, opname)].append(("self", op, a))
                elif tgt and tgt.startswith("ext:"):
                    out.append({"do": f"Call {actor_part[tgt[4:]]}: {op}({a})."})
                    for inst in parts_of.get(comp) or []:  # every instance; the program's start-up is no part
                        pair_calls.setdefault((inst, tgt), OrderedDict()).setdefault(op, a)
                    op_calls[(comp, opname)].append((tgt, op, a))
                elif tgt:
                    out.append({"do": f"Call {tgt}.{op}({a})."})
                    for inst in parts_of.get(comp) or []:  # every instance of the caller makes the call, through its own field
                        t2 = target_of(comp, inst, s)[0] or tgt
                        if t2 != "self":
                            pair_calls.setdefault((inst, t2), OrderedDict()).setdefault(op, a)
                    op_calls[(comp, opname)].append((tgt, op, a))
                else:
                    out.append({"do": f"{s.get('call')}({a})."})
                c["calls"] += 1
            elif "if" in s:
                cond = str(s["if"]).strip().rstrip(".")
                m = re.match(r"(?:the call to )?(\w+) fails$", cond)
                neg = re.fullmatch(r"not\s+(?!\()(.+)", cond)  # "not user.check_password(...)": its opposite drops the not
                yes, no = (f"{m.group(1)} fails (returns an error)", f"{m.group(1)} succeeds") if m else (
                    cond, neg.group(1) if neg else f"not ({cond})")
                then = convert(comp, pk, opname, s.get("then", []), c)
                other = convert(comp, pk, opname, s.get("else", []), c)
                failure = bool(re.search(r" fails\b", yes))
                if failure and not ends(then):
                    then.append({"do": f"Go on; the failure does not end {opname}."})
                out.append({"if": yes, "otherwise": no, "then": then, "else": other})
                c["checks"] += 1
            elif "switch" in s:
                cases = [{"case": str(x.get("case", "")), "steps": convert(comp, pk, opname, x.get("steps", []), c)}
                         for x in s.get("cases", []) if isinstance(x, dict)]
                out.append({"switch": str(s["switch"]), "cases": cases})
                c["checks"] += 1
            elif "loop" in s:
                body = convert(comp, pk, opname, s.get("steps", []), c)
                if body:
                    out.append({"loop": str(s["loop"]).removeprefix("For ").removeprefix("for "), "steps": body})
            elif "return" in s:
                what = str(s.get("return") or "").strip()
                what = re.sub(r"^nil,\s*|,\s*nil$", "", what).strip()  # Go's (value, nil) and (nil, err)
                # err, Err..., newErr, an exception, a failure; not the "erR" of DataFetcherResult
                err = re.search(r"(?<![A-Za-z])[eE]rr|(?<=[a-z])Err|(?i:exception|fail)", what)
                last = top and s is steps[-1]
                responded = any("respond" in x for x in steps[:steps.index(s)])
                if (comp, opname) in route_of and responded:  # a handler that has answered just ends
                    out.append({"stop": True, "text": "Stop."})
                    continue
                if err and last and re.fullmatch(r"(?i)err\w*|error", what):  # Go's named results at the end: it just ends
                    text = "Return."
                else:
                    text = "Stop and return the error." if err else (f"Return {what}." if what and what not in ("nothing", "void", "none") else
                                                                     ("Stop." if (comp, opname) in route_of else "Return."))
                out.append({"stop": True, "text": text})
            elif "respond" in s:
                code = str(s["respond"])
                out.append({"do": f"Respond {code}.", "respond": int(code) if code.isdigit() else code})
                c["responds"] += 1
            elif "log" in s:
                lvl = "an error" if str(s.get("level", "")).lower().startswith("err") else "a warning"
                out.append({"do": f'Log {lvl} ("{s["log"]}").'})
            elif "set" in s:
                a, _, b = str(s["set"]).partition("=")
                out.append({"do": f"Set {a.strip()} to {b.strip()}." if b else f"Set {a.strip()}."})
            elif "skip" in s:
                out.append({"do": SKIP})
            elif "background" in s:
                m = re.search(r"(\w+)\s*\(", str(s["background"])) or re.search(r"(\w+)\s*$", str(s["background"]))
                bg = m.group(1) if m else str(s["background"])  # s.processAsync(ctx, id) -> processAsync
                out.append({"do": f"Start {bg} in the background and go on without waiting for it.", "background": bg})
                c["forks"] += 1
                op_calls[(comp, opname)].append(("spawn", bg, ""))
        return out

    def ends(steps: list) -> bool:
        return bool(steps) and ("stop" in steps[-1] or steps[-1].get("do") in (SKIP,))

    spawned = set()
    for comp in order + ([SYSTEM] if SYSTEM in r.behaviour else []):
        beh = r.behaviour.get(comp) or {}
        pk = parts_of[comp][0] if parts_of.get(comp) else d["system"]["part"]
        for o in beh.get("operations", []):
            if o.get("name") not in info(comp)["operations"]:
                continue
            key = (comp, o["name"])
            op_calls[key] = []
            counts[key] = Counter()
            steps = convert(comp, pk, o["name"], o.get("steps", []), counts[key], top=True)
            rt = route_of.get(key)
            if rt and steps:
                steps.insert(0, {"do": f"Receive the request ({rt['method']} {rt['path']})."})
            op_steps[key] = steps
            for t, op, _ in op_calls[key]:
                if t == "spawn":
                    spawned.add((comp, op))

    if synthetic:  # the router's one activity: the middleware in order, then the handler of the route
        served = OrderedDict()
        for rt in routes:
            hp = parts_of.get(rt["comp"] or "", [None])[0] if rt["comp"] else None
            if hp:
                served.setdefault(hp, []).append(f"{rt['method']} {rt['path']}")
        steps = [{"do": "Receive the request (any route below the mount prefix)."}]
        steps += [{"do": f"{m['name']}: {m.get('what') or m['name']}" + (f" ({m.get('file')}:{m.get('line')})" if m.get("file") else "")}
                  for m in r.middleware]
        steps.append({"switch": "the request's route", "cases": [{"case": ", ".join(p), "steps": [{"do": f"Pass the request to {h}."}]}
                                                                 for h, p in served.items()]})
        op_steps[(synthetic, "HandleRequest")] = steps
        counts[(synthetic, "HandleRequest")] = Counter(calls=len(served), checks=1)
        op_calls[(synthetic, "HandleRequest")] = []

    # a system with no routes (a device, a library) is used through the operations nothing inside it calls: each that
    # reaches another part or an outside system is an entry its user calls, and an interaction of its own
    # the other ways in: RPC methods, GraphQL queries and mutations, commands, message consumers, scheduled jobs. The
    # map's, then any operation nothing in the system calls whose annotation or decorator says how its framework calls
    # it (@DgsMutation, @KafkaListener, @Scheduled); each an entry the client of its kind calls
    ways = [(str(m.get("method", "")).upper(), m.get("component"), m.get("operation"), str(m.get("path") or ""))
            for m in r.map_routes]
    reached = {(r.parts[t]["comp"], o) for calls in op_calls.values() for t, o, _ in calls if t in r.parts}
    reached |= {(rt["comp"], rt["op"]) for rt in routes} | {(m[1], m[2]) for m in ways}
    for n in order:
        for oname, o in r.comps[n]["operations"].items():
            kind = annotated_kind(r, r.op_fn(r.comps[n], o)) if (n, oname) not in reached else ""
            if kind:
                ways.append((kind, n, oname, oname))
    entries = []
    for kind, n, oname, path in ways:
        if kind in HTTP or n not in r.comps or oname not in r.comps[n]["operations"] or not parts_of.get(n) \
                or any(e[:2] == (n, oname) for e in entries):
            continue
        actor, what = next(((a, w) for kinds, a, w in ENTRY_ACTORS if kind in kinds), ("Client", "calls the system's entry points"))
        if actor not in actors:
            actors[actor] = {"name": actor, "what": what, "clients": set()}
            actor_part[actor] = camel(actor)
        label = f"{kind} {path or oname}".strip()
        pair_calls.setdefault((f"ext:{actor}", parts_of[n][0]), OrderedDict()).setdefault(label, "")
        entries.append((n, oname, actor, label))

    # a system with no routes (a device, a library) is used through the operations nothing inside it calls: each that
    # reaches another part or an outside system is an entry its user calls, and an interaction of its own
    if not routes:
        called = {b for (a, b) in pair_calls if not a.startswith("ext:")}
        typed = {e[:2] for e in entries}
        users = []
        for n in order:
            if any(p in called for p in parts_of[n]):
                continue
            inside = {op for (c2, _), calls in op_calls.items() if c2 == n for t, op, _ in calls if t == "self"}
            for oname, o in r.comps[n]["operations"].items():
                if oname.startswith("_") or o.get("internal") or oname in inside or (n, oname) in typed:
                    continue  # a step of its own operations, or a way in of another kind
                if any(t not in ("self", "spawn") for t, _, _ in op_calls.get((n, oname), [])) and parts_of[n]:
                    users.append((n, oname))
        if users:
            actors["User"] = {"name": "User", "what": "uses the system: calls its operations", "clients": set()}
            actor_part["User"] = "user"
            for n, oname in users:
                pair_calls.setdefault(("ext:User", parts_of[n][0]), OrderedDict()).setdefault(oname, "")
        entries += [(n, oname, "User", f"{oname}()") for n, oname in users]

    # routes: the API client calls the router (or the handlers), the router passes each request on
    for rt in routes:
        hp = parts_of.get(rt["comp"] or "", [None])[0] if rt["comp"] else None
        if hp is None:
            continue
        if router and router != hp:
            pair_calls.setdefault(("ext:ApiClient", router), OrderedDict()).setdefault(f"{rt['method']} {rt['path']}", "")
            pair_calls.setdefault((router, hp), OrderedDict()).setdefault(f"{rt['method']} {rt['path']}", "")
        else:
            pair_calls.setdefault(("ext:ApiClient", hp), OrderedDict()).setdefault(f"{rt['method']} {rt['path']}", "")

    def pname(x: str) -> str:
        return actor_part[x[4:]] if x.startswith("ext:") else x

    def cname(x: str) -> str:
        return x[4:] if x.startswith("ext:") else r.parts[x]["comp"]

    # an adapter nothing connects to, whose operations are only plumbing (a ResponseWriter or logger wrapper), is no part
    linked = {cname(x) for pair in pair_calls for x in pair if not x.startswith("ext:")} | ({router_name} if router_name else set())
    for n in list(order):
        ops = set(r.comps[n]["operations"])
        if n not in linked and not any(rt["comp"] == n for rt in routes) and ops and ops <= PLUMBING:
            order.remove(n)
    d["components"] = []
    for n in order:
        c = r.comps[n]
        d["components"].append({"id": ids[n], "name": n, "def": defs[n], "subsystem": subs[n],
                                "parts": [{"name": k, "over": r.parts[k]["over"], "built": r.parts[k]["built"]} for k in parts_of[n]],
                                "purpose": str(c.get("purpose", "")).rstrip("."),
                                "routes": [f"{rt['method']} {rt['path']}" for rt in routes if rt["comp"] == n],
                                "operations": [o + (" (background)" if (n, o) in spawned else "") for o in c["operations"]],
                                "evidence": f"{c.get('file')}:{(c.get('lines') or [0])[0]}"})
    d["subsystems"] = []
    for i, s in enumerate(OrderedDict.fromkeys(v for v in (subs[n] for n in order) if v), 1):
        d["subsystems"].append({"id": f"SUB_{i:02d}", "name": s, "components": [ids[n] for n in order if subs[n] == s],
                                "folders": sorted({os.path.dirname(r.comps[n].get("file", "")) for n in order if subs[n] == s})})
    d["actors"] = []
    for i, (a, v) in enumerate(actors.items(), 1):
        talks = sorted({pname(x) for (x, y) in pair_calls if y == "ext:" + a} | {pname(y) for (x, y) in pair_calls if x == "ext:" + a})
        d["actors"].append({"id": f"ACTOR_{i:02d}", "name": a, "part": actor_part[a], "what": v["what"], "talks_to": talks,
                            "evidence": sorted(x for x in v["clients"] if x)})
    d["connections"] = []
    for i, ((a, b), calls) in enumerate(pair_calls.items(), 1):
        sa = subs.get(cname(a), "") if not a.startswith("ext:") else ""
        sb = subs.get(cname(b), "") if not b.startswith("ext:") else ""
        d["connections"].append({"id": f"CONN_{i:02d}", "from": pname(a), "to": pname(b), "from_def": cname(a), "to_def": cname(b),
                                 "subsystems": f"{sa} → {sb}" if sa and sb and sa != sb else (sa or sb or ""),
                                 "calls": [{"name": op, "args": [x.strip() for x in args.split(",") if x.strip()] if args else [],
                                            "result": "HTTP response" if a == "ext:ApiClient" or (router and a == router) else ""}
                                           for op, args in calls.items()]})
    # operations, activities
    opid, k = {}, 0
    d["operations"] = []
    for n in order + ([SYSTEM] if SYSTEM in r.behaviour else []):
        for oname, o in info(n)["operations"].items():
            k += 1
            opid[(n, oname)] = f"OP_{k:02d}"
            fn = r.op_fn(info(n), o)
            shown = "Startup" if n == SYSTEM and oname.lower() == "main" else oname
            d["operations"].append({"id": opid[(n, oname)], "name": shown, "owner": title if n == SYSTEM else n,
                                    "args": [str(x) for x in o.get("inputs", []) or []][:8], "result": str(o.get("output", "") or ""),
                                    "background": (n, oname) in spawned or bool(o.get("background")),
                                    "flow": False, "evidence": f"{fn.file}:{fn.line}" if fn else f"{o.get('file')}:{(o.get('lines') or [0])[0]}",
                                    "doc": ""})
    d["activities"] = []
    for (n, oname), steps in op_steps.items():
        cnt = counts[(n, oname)]
        rt = route_of.get((n, oname))
        bg = (n, oname) in spawned
        way_in = rt or any(e[:2] == (n, oname) for e in entries)
        if not steps or not (bg or cnt["forks"] or (way_in and cnt["calls"] + cnt["checks"] >= 1) or cnt["calls"] + cnt["checks"] >= 3):
            continue
        op = next(o for o in d["operations"] if o["id"] == opid[(n, oname)])
        op["flow"] = True
        d["activities"].append({"id": opid[(n, oname)], "name": op["name"], "owner": op["owner"], "route": f"{rt['method']} {rt['path']}" if rt else "",
                                "background": bg, "steps": steps, "evidence": op["evidence"]})
    d["activities"].sort(key=lambda a: int(a["id"][3:]))
    d["routes"] = []
    for rt in routes:
        hp = parts_of.get(rt["comp"] or "", [None])[0] if rt["comp"] else None
        steps = op_steps.get((rt["comp"], rt["op"]), [])
        codes = sorted({int(x["respond"]) for x in flat(steps) if isinstance(x.get("respond"), int)})
        d["routes"].append({"method": rt["method"], "path": rt["path"], "handler": f"{hp}.{rt['op']}" if hp else "",
                            "op": opid.get((rt["comp"], rt["op"]), ""), "request": [], "codes": codes, "response": [],
                            "evidence": f"{rt['file']}:{rt['line']}"})

    # interactions: each route and each background task, followed through the calls
    def follow(comp: str, pk: str, oname: str, msgs: list, depth: int, stack: set) -> None:
        for t, op, a in op_calls.get((comp, oname), []):
            if t == "spawn":
                continue
            if t == "self":
                if info(comp)["operations"].get(op, {}).get("internal"):
                    continue  # a private status setter (_enter): state plumbing, not an exchange
                if depth < 6 and (comp, op) not in stack:
                    follow(comp, pk, op, msgs, depth + 1, stack | {(comp, op)})
                continue
            msgs.append((pk, pname(t), f"{op}({a})"))
            if not t.startswith("ext:"):
                tc = r.parts[t]["comp"]
                if depth < 6 and (tc, op) not in stack:
                    follow(tc, t, op, msgs, depth + 1, stack | {(tc, op)})
                msgs.append((t, pk, f"{op} returns"))

    d["interactions"] = []
    seqs = []
    for rt in routes:
        hp = parts_of.get(rt["comp"] or "", [None])[0] if rt["comp"] else None
        if hp is None or (rt["comp"], rt["op"]) not in op_steps:
            continue
        label = f"{rt['method']} {rt['path']}"
        msgs = [("apiClient", router, f"{label} request"), (router, hp, f"{label} request")] if router and router != hp else [("apiClient", hp, f"{label} request")]
        follow(rt["comp"], hp, rt["op"], msgs, 0, {(rt["comp"], rt["op"])})
        msgs += [(hp, router, "HTTP response"), (router, "apiClient", "HTTP response")] if router and router != hp else [(hp, "apiClient", "HTTP response")]
        name = pascal(re.sub(r"[{}:]", "", rt["path"]) or "root") + pascal(rt["method"].lower())
        seqs.append((name, label, [label], msgs, rt["comp"], rt["op"]))
    for comp, op in sorted(spawned):
        if (comp, op) in op_steps and parts_of.get(comp):
            pk = parts_of[comp][0]
            msgs = []
            follow(comp, pk, op, msgs, 0, {(comp, op)})
            if msgs:
                seqs.append((pascal(op) + "InBackground", f"{comp}.{op}, in the background", [], msgs, comp, op))
    for comp, op, actor, label in entries:
        pk = parts_of[comp][0]
        msgs = [(actor_part[actor], pk, label)]
        follow(comp, pk, op, msgs, 0, {(comp, op)})
        seqs.append((pascal(op) if actor == "User" else pascal(label.split(" ")[0].lower()) + pascal(label.partition(" ")[2] or op),
                     f"the user calls {comp}.{op}" if actor == "User" else f"{actor} sends {label} to {comp}.{op}", [], msgs, comp, op))
    for i, (name, label, rts, msgs, comp, op) in enumerate(seqs, 1):
        parts = list(OrderedDict.fromkeys(x for a, b, _ in msgs for x in (a, b)))
        spawns = [o for t, o, _ in op_calls.get((comp, op), []) if t == "spawn"]
        d["interactions"].append({"id": f"SEQ_{i:02d}", "name": name, "trigger": label, "routes": rts, "participants": parts,
                                  "messages": [{"from": a, "to": b, "what": w} for a, b, w in msgs], "spawns": spawns})

    # state machines: the status values the operations set
    d["state_machines"] = []
    st_n = tr_n = 0
    trans_by_enum: dict[str, list] = OrderedDict()

    def sets_value(comp: str, fn, line, value: str) -> bool:
        """The cited line sets the value, or calls an operation of the same component whose body sets it (setFailed)."""
        if r.near(fn.file, line, value, 2):
            return True
        for callee, at in fn.calls:
            if isinstance(line, int) and abs(at - line) <= 1:
                helper = next((h for h in r.outline.files[fn.file].fns + r.outline.methods(info(comp).get("code", ""))
                               if h.name == callee.split(".")[-1]), None)
                if helper is not None and any(value in r.outline.line(helper.file, x) for x in range(helper.line, helper.end + 1)):
                    return True
        return False

    for comp in order:
        for t in (r.behaviour.get(comp) or {}).get("transitions", []):
            e = r.enums.get(str(t.get("status", "")))
            if e is None or not t.get("to"):
                continue
            fn = None
            o = r.comps[comp]["operations"].get(str(t.get("operation", "")))
            if o is not None:
                fn = r.op_fn(r.comps[comp], o)
            to_val = str(t["to"]).split(".")[-1]
            if fn is None or not sets_value(comp, fn, t.get("line"), to_val):
                r.problems.append(f"{comp}: transition to {to_val} not on line {t.get('line')}")
                continue
            trans_by_enum.setdefault(e["name"], []).append((comp, t, fn))
    # a status set in a helper (setFailed) changes state where the helper is called: one transition per call,
    # when the check around that call holds ("processAsync: Parse fails")
    for ename, ts in trans_by_enum.items():
        expanded = []
        for comp, t, fn in ts:
            helper = str(t.get("operation"))
            callers = [(k[1], st) for k, st in op_steps.items() if k[0] == comp and k[1] != helper
                       and any(c[0] == "self" and c[1] == helper for c in op_calls.get(k, []))]
            # a setter the callers pass the state to (_enter(State.IDLE)) is no fixed transition: each caller's own
            # change is reported where it calls it, and the helper's own check stays one transition
            values = [str(v.get("name")).split(".")[-1] for v in r.enums[ename].get("values", []) if isinstance(v, dict)]
            passes_state = any(c[0] == "self" and c[1] == helper and any(v in str(c[2]) for v in values)
                               for k in op_calls for c in op_calls[k] if k[0] == comp)
            if not callers or passes_state:
                expanded.append((comp, t, fn))
                continue
            for caller, steps in callers:
                o = info(comp)["operations"].get(caller)
                cfn = r.op_fn(info(comp), o) if o else None
                sites = [line for callee, line in (cfn.calls if cfn else []) if callee.split(".")[-1] == helper]
                conds = [s["if"] for s in flat(steps) if "if" in s and any(x.get("do", "").startswith(f"Run {helper}(") for x in s["then"])]
                for k, line in enumerate(sites):
                    cond = conds[k] if k < len(conds) else t.get("when", "")
                    expanded.append((comp, dict(t, operation=caller, when=cond, line=line), cfn))
        trans_by_enum[ename] = expanded
    for ename, ts in trans_by_enum.items():  # "AWAITING_PIN or MENU": one transition from each
        split = []
        for comp, t, fn in ts:
            froms = [x for x in re.split(r"\s+or\s+|\s*[,/|]\s*|\s+and\s+", str(t.get("from") or "")) if x.strip()]
            split += [(comp, dict(t, **{"from": x.strip()}), fn) for x in froms] if len(froms) > 1 else [(comp, t, fn)]
        trans_by_enum[ename] = split
    for i, (ename, ts) in enumerate(trans_by_enum.items(), 1):
        e = r.enums[ename]
        values = [v for v in e.get("values", []) if isinstance(v, dict) and v.get("name")]
        sname = {str(v["name"]): camel(str(v["name"])) for v in values}
        states = []
        for v in values:
            st_n += 1
            states.append({"id": f"ST_{st_n:02d}", "name": sname[str(v["name"])], "code": f"{v['name']} = {v.get('value', '')}"})
        owner = Counter(c for c, _, _ in ts).most_common(1)[0][0]
        # the initial state: what a record is created with (no state before it)
        initial = None
        for comp, t, fn in ts:
            if str(t.get("from") or "").strip().lower() in ("none", "null", "nil", "-", "initial", "start", "new record"):
                t["from"] = None
        for comp, t, fn in ts:
            if not t.get("from") and re.search(r"creat|insert|new|add|regist|submit|open|init", str(t.get("operation", "")) + " "
                                               + str(t.get("when", "")), re.I):
                initial = {"state": sname.get(str(t["to"]).split(".")[-1], camel(str(t["to"]).split(".")[-1])),
                           "where": f"{t.get('operation')} (line {t.get('line')})"}
                break
        trans, last_in_op = [], {}
        for comp, t, fn in sorted(ts, key=lambda x: (str(x[1].get("operation")), int(x[1].get("line") or 0))):
            to = sname.get(str(t["to"]).split(".")[-1], camel(str(t["to"]).split(".")[-1]))
            op = str(t.get("operation"))
            frm = sname.get(str(t.get("from") or "").split(".")[-1]) if t.get("from") else None
            if initial and to == initial["state"] and frm is None and op in initial["where"]:
                continue  # the creation itself is the entry, not a transition
            if frm is None:  # the state the same operation set before (not on a branch that ends it), else the start
                earlier = [x for x in last_in_op.get(op, []) if x[0] < int(t.get("line") or 0)]
                frm = earlier[-1][1] if earlier else (initial["state"] if initial else None)
            if " fails" not in str(t.get("when", "")):
                last_in_op.setdefault(op, []).append((int(t.get("line") or 0), to))
            if frm is None or frm == to:
                continue
            tr_n += 1
            eff = next((f"{tgt}.{cop} saves status {to}" for tgt, cop, _ in op_calls.get((comp, op), [])
                        if not tgt.startswith(("ext:", "self", "spawn")) and r.near(fn.file, t.get("line"), cop, 3)), "")
            trans.append({"id": f"TR_{tr_n:02d}", "from": frm, "to": to, "when": f"{op}: {t.get('when', '')}",
                          "effect": eff, "evidence": f"{fn.file}:{t.get('line')}", "method": op})
        if not trans:
            continue
        d["state_machines"].append({"id": f"SM_{i:02d}", "name": pascal(ename) + "States", "owner": owner,
                                    "held_in": "", "states": states,
                                    "initial": initial or ({"state": states[0]["name"], "where": ""} if states else None),
                                    "transitions": trans, "evidence": f"{e.get('file')}:{(e.get('lines') or [0])[0]}"})

    # settings, limits, data
    d["settings"] = []
    for i, s in enumerate(r.settings, 1):
        owner = s.get("owner") if s.get("owner") in r.comps else title
        name = camel(str(s.get("field") or s["key"]).split(".")[-1])
        default = s.get("default")
        value = '""' if default == "" or default == '""' else (str(default) if default not in (None, "") else "(none)")
        d["settings"].append({"id": f"VAL_{i:02d}", "owner": owner, "name": name, "value": value,
                              "env": str(s["key"]), "section": ".".join(str(s.get("field", "")).split(".")[:-1]),
                              "evidence": f"{s.get('file')}:{s.get('line')}"})
    base = len(d["settings"])
    d["limits"] = []
    for i, x in enumerate(r.limits, 1):
        owner = x.get("owner") if x.get("owner") in r.comps else title
        name = camel(str(x.get("name") or ""))
        if not name or not name[0].isalpha():  # a bare number: name it after the call or the name it is given to
            line = r.outline.line(x.get("file", ""), int(x.get("line") or 0)) if isinstance(x.get("line"), int) else ""
            m = re.search(rf"(\w+)\s*(?:\(|:=|=|:)[^;]*?{re.escape(str(x.get('raw') or x['value']))}", line)
            name = camel(m.group(1)) + "Limit" if m and not m.group(1)[0].isdigit() else f"limit{i}"
        d["limits"].append({"id": f"VAL_{base + i:02d}", "owner": owner, "name": name,
                            "value": str(x.get("value")), "what": f"limit {x.get('name')}", "evidence": f"{x.get('file')}:{x.get('line')}"})
    data = []
    for e in r.enums.values():
        vals = [f"{camel(str(v['name']))} ({v.get('value', '')})" for v in e.get("values", []) if isinstance(v, dict) and v.get("name")]
        if vals:
            data.append({"name": pascal(e["name"]), "kind": "enum", "values": vals, "code": e["name"],
                         "evidence": f"{e.get('file')}:{(e.get('lines') or [0])[0]}"})
    for x in r.data.values():
        fields = []
        for f in x.get("fields", []) or []:
            nm, _, ty = str(f).partition(":")
            fields.append(f"{camel(nm.strip())}: {type_word(ty.strip())}")
        data.append({"name": pascal(x["name"]), "kind": "data", "fields": fields, "code": x["name"],
                     "evidence": f"{x.get('file')}:{(x.get('lines') or [0])[0]}"})
    d["data"] = [{"id": f"DATA_{i:02d}", **v} for i, v in enumerate(data, 1)]
    d["ids"] = {"comp": ids}
    # where each piece of code is, for anchoring requirements
    spans = []
    for n in order:
        c = r.comps[n]
        ln = c.get("lines") or [0, 0]
        if c.get("file"):
            spans.append((c["file"], int(ln[0] or 0), int(ln[-1] or 0), "", n))
        for oname, o in c["operations"].items():
            fn = r.op_fn(c, o)
            if fn is not None:
                spans.append((fn.file, fn.line, fn.end, opid[(n, oname)], n))
    for rt in routes:
        if rt["comp"] and rt.get("file"):
            spans.append((rt["file"], int(rt["line"] or 0), int(rt["line"] or 0), "", rt["comp"]))
    for sm in d["state_machines"]:
        for t in sm["transitions"]:
            rel, line = t["evidence"].rsplit(":", 1)
            spans.append((rel, int(line), int(line), t["id"], sm["owner"]))
    d["spans"] = spans
    d["decisions"] = [
        f"Read by the model and checked against the code ({r.calls} model calls): every component, operation, step, setting and "
        "status change cites its lines, and an item not found on its lines was asked again, then left out.",
        "Parts are what the code builds (its constructors, factories and dependency injection); a component built once is one "
        "part named after it, one built several times has a part per build.",
        "Connections are the calls a component makes on what it is given; each carries the operations called.",
        "An activity is an operation with steps: a route's handler, background work, or one with three or more calls and checks; "
        "a failure the code carries on from says so.",
    ] + ([f"Left out after checking: {len(r.problems)} items (" + "; ".join(r.problems[:5]) + ("…" if len(r.problems) > 5 else "") + ")."]
         if r.problems else [])
    return d


def type_word(t: str) -> str:
    """A field type in the words the design uses: String, Integer, Real, Boolean, a record's name, with [*] for lists."""
    many = bool(re.search(r"\[\]|\[\*\]|List|list\[|Array|\[\s*\]|Set|Sequence|Iterable|IEnumerable|\*\]", t or ""))
    b = re.sub(r"[\[\]*?]|\b(List|list|Array|Set|Optional|Sequence|Iterable|IEnumerable|Promise|Readonly)\b|<|>|\(|\)", " ", t or "").split()
    base = b[-1] if b else "String"
    low = base.lower()
    word = ("String" if low in ("str", "string", "text", "uuid", "datetime", "date", "time", "bytes", "byte", "char", "object", "any", "json")
            else "Integer" if low in ("int", "int32", "int64", "long", "short", "integer", "uint", "uint64", "number", "bigint")
            else "Real" if low in ("float", "float32", "float64", "double", "decimal", "real")
            else "Boolean" if low in ("bool", "boolean") else pascal(base.split(".")[-1]))
    return word + ("[*]" if many else "")


def minor(s: dict) -> bool:
    """A step that only sets a value or logs, or a loop of such steps."""
    if set(s) & {"set", "log"}:
        return True
    if "loop" in s:
        body = [x for x in s.get("steps", []) or [] if isinstance(x, dict)]
        return all(minor(x) for x in body)
    return False


def flat(steps: list) -> list:
    out = []
    for s in steps:
        out.append(s)
        for key in ("then", "else", "steps"):
            out += flat(s.get(key, []))
        for c in s.get("cases", []):
            out += flat(c.get("steps", []))
    return out
