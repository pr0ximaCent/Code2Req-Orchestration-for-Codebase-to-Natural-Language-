import os

from code2req.surfaces import detect_surfaces
from code2req.surfaces.python import detect_python

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


def test_unparsable_file_is_skipped():
    assert detect_python("def broken(:\n", "bad.py") == []
