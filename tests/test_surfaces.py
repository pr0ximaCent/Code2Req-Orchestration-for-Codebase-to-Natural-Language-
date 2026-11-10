import os

from code2req.surfaces import detect_surfaces
from code2req.surfaces.python import detect_python
from code2req.surfaces.typescript import detect_typescript

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "shop")


def by_kind(surfaces, kind):
    return {s.name: s for s in surfaces if s.kind == kind}


def test_fastapi_routes_env_constants_models():
    surfaces = detect_surfaces(FIXTURE)
    routes = by_kind(surfaces, "http_route")
    assert set(routes) == {"POST /cart/items", "GET /orders/{order_id}"}
    assert routes["POST /cart/items"].file == "app/api.py"
    assert routes["POST /cart/items"].line == 24
    assert set(by_kind(surfaces, "env_var")) == {"SHOP_API_KEY"}
    consts = by_kind(surfaces, "constant")
    assert consts["MAX_CART_ITEMS"].detail == "50"
    assert "SESSION_TIMEOUT_MS" in consts
    assert set(by_kind(surfaces, "model")) == {"CartItem"}
    tests = by_kind(surfaces, "test")
    assert tests["test_validate_quantity_rejects_zero"].file == "tests/test_api.py"


def test_flask_route_methods_and_click_commands():
    src = '''
import click
from flask import Flask
app = Flask(__name__)

@app.route("/login", methods=["POST"])
def login():
    return ""

@click.command(name="sync")
def sync_cmd():
    pass

@cli.command()
def export():
    pass
'''
    surfaces = detect_python(src, "svc.py")
    assert set(by_kind(surfaces, "http_route")) == {"POST /login"}
    assert set(by_kind(surfaces, "cli_command")) == {"sync", "export"}


def test_environ_subscript_and_get():
    src = 'import os\nA = os.environ["DB_URL"]\nB = os.environ.get("DEBUG", "0")\n'
    assert set(by_kind(detect_python(src, "c.py"), "env_var")) == {"DB_URL", "DEBUG"}


def test_enums_and_error_types():
    src = (
        "from enum import Enum\n"
        "class NodeType(str, Enum):\n    FRAME = 'act::frame'\n"
        "class AuthError(Exception):\n    pass\n"
        "class LockedError(AuthError):\n    pass\n"
        "class Plain:\n    pass\n"
    )
    surfaces = detect_python(src, "defs.py")
    assert set(by_kind(surfaces, "enum")) == {"NodeType"}
    assert set(by_kind(surfaces, "error")) == {"AuthError", "LockedError"}
    assert "Plain" not in {s.name for s in surfaces}


def test_unparsable_file_is_skipped():
    assert detect_python("def broken(:\n", "bad.py") == []


NEXT_ROUTE = '''
import { NextRequest } from "next/server";
export async function GET(req: NextRequest) { return new Response("ok"); }
export const POST = async (req: NextRequest) => new Response("ok");
const handler = (req: NextRequest) => new Response("ok");
export { handler as PUT };
export const dynamic = "force-dynamic";
'''


def test_nextjs_app_router_routes_come_from_folders_and_exports():
    ss = detect_typescript(NEXT_ROUTE, "src/app/api/projects/[projectId]/export/route.ts")
    routes = by_kind(ss, "http_route")
    assert set(routes) == {"GET /api/projects/{projectId}/export", "POST /api/projects/{projectId}/export",
                           "PUT /api/projects/{projectId}/export"}
    assert routes["GET /api/projects/{projectId}/export"].line == 3
    # (group) and @slot folders are not in the URL; [...path] is one parameter; _private folders are not routed
    assert set(by_kind(detect_typescript(NEXT_ROUTE, "app/(admin)/@panel/files/[...path]/route.ts"),
                       "http_route")) == {"GET /files/{path}", "POST /files/{path}", "PUT /files/{path}"}
    assert detect_typescript(NEXT_ROUTE, "app/_lib/route.ts") == []
    assert detect_typescript(NEXT_ROUTE, "src/lib/route.ts") == []


def test_nextjs_route_path_is_found_when_the_run_is_on_a_subfolder(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    folder = tmp_path / "src" / "app" / "api" / "projects" / "[projectId]"
    (folder / "export").mkdir(parents=True)
    (folder / "export" / "route.ts").write_text(NEXT_ROUTE)
    routes = by_kind(detect_surfaces(str(folder)), "http_route")
    assert "GET /api/projects/{projectId}/export" in routes
    assert routes["GET /api/projects/{projectId}/export"].file == "export/route.ts"


def test_pages_api_and_express_style_routes():
    page = "export default function handler(req, res) { res.status(200).json({}); }\n"
    assert set(by_kind(detect_typescript(page, "src/pages/api/users/[id].ts"), "http_route")) == {"ANY /api/users/{id}"}
    assert set(by_kind(detect_typescript(page, "pages/api/index.js"), "http_route")) == {"ANY /api"}
    assert detect_typescript(page, "pages/about.tsx") == []
    src = '''
// app.get("/commented", h)
app.get("/users/:id", auth, (req, res) => res.json({}));
apiRouter.post(`/items`, createItem);
this.app.all("/health", ok);
api.get("/client/call", config);      // a client calling a backend, not a route
app.get("port");                      // an Express setting
router.get(`/x/${id}`, h);            // a built path is not a route literal
'''
    assert set(by_kind(detect_typescript(src, "server/index.js"), "http_route")) == {
        "GET /users/:id", "POST /items", "ANY /health"}
