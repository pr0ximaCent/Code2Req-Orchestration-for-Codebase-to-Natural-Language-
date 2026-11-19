"""Code rules of the design reader that need no model: full route paths from nested routers (FastAPI, Express), the
function a route decorator is written over, settings a framework reads from the environment, and objects an operation
builds for one call."""
import os

from code2req.design.mounts import Mounts
from code2req.design.reader import Reader, source_files

FASTAPI = {
    "app/main.py": """from fastapi import FastAPI

from app.api.api import router as api_router
from app.core.settings import get_settings

settings = get_settings()
application = FastAPI()
application.include_router(api_router, prefix=settings.api_prefix)
""",
    "app/core/settings.py": """from pydantic_settings import BaseSettings


class BaseAppSettings(BaseSettings):
    debug: bool = False


class AppSettings(BaseAppSettings):
    api_prefix: str = "/api"
    max_connections: int = 10


def get_settings() -> AppSettings:
    return AppSettings()
""",
    "app/api/api.py": """from fastapi import APIRouter

from app.api.routes import users
from app.api.routes.articles import api as articles

router = APIRouter()
router.include_router(users.router, tags=["users"], prefix="/users")
router.include_router(articles.router)
""",
    "app/api/routes/users.py": """from fastapi import APIRouter

from app.services import jwt
from app.services.validation import SignupValidator

router = APIRouter()


@router.post(
    "",
    status_code=201,
    name="users:register",
)
async def register(user: dict) -> dict:
    SignupValidator(user).check()
    return {"token": jwt.create_token(user)}


@router.post("/login")
async def login(user: dict) -> dict:
    return {"token": jwt.create_token(user)}
""",
    "app/api/routes/articles/api.py": """from fastapi import APIRouter

from app.api.routes.articles import articles_resource

router = APIRouter()
router.include_router(articles_resource.router, prefix="/articles")
""",
    "app/api/routes/articles/articles_resource.py": """from fastapi import APIRouter

router = APIRouter()


@router.get("/{slug}")
async def get_article(slug: str) -> dict:
    return {"slug": slug}
""",
    "app/services/validation.py": """class SignupValidator:
    def __init__(self, user):
        self.user = user

    def check(self):
        return bool(self.user)
""",
    "app/services/jwt.py": """def create_token(user):
    return "token"
""",
}

EXPRESS = {
    "src/main.ts": """import express from 'express';
import routes from './routes/routes';

const app = express();
app.use(express.json());
app.use(routes);

app.get('/', (req, res) => {
  res.json({ status: 'up' });
});
""",
    "src/routes/routes.ts": """import { Router } from 'express';
import articlesController from './article.controller';

const api = Router()
  .use(articlesController);

export default Router().use('/api', api);
""",
    "src/routes/article.controller.ts": """import { Router } from 'express';

const router = Router();

router.get(
  '/articles/feed',
  async (req, res) => {
    res.json([]);
  },
);

router.post('/articles', async (req, res) => {
  res.status(201).json({});
});

export default router;
""",
}


def write(root, files: dict) -> str:
    for rel, text in files.items():
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return str(root)


def line_of(text: str, needle: str) -> int:
    return next(i for i, x in enumerate(text.splitlines(), 1) if needle in x)


def test_fastapi_routes_get_every_prefix_above_them(tmp_path):
    r = Reader(write(tmp_path, FASTAPI), {"surfaces": []}, "T", None, source_files(str(tmp_path)))
    m = Mounts(r.outline)
    users = FASTAPI["app/api/routes/users.py"]
    # "" under prefix="/users" under the setting api_prefix = "/api"; cited at the decorator or at the def under it
    assert m.full_path("app/api/routes/users.py", line_of(users, "@router.post(")) == "/api/users"
    assert m.full_path("app/api/routes/users.py", line_of(users, "async def register")) == "/api/users"
    assert m.full_path("app/api/routes/users.py", line_of(users, '"/login"')) == "/api/users/login"
    # mounted with no prefix, whose own child has one
    art = FASTAPI["app/api/routes/articles/articles_resource.py"]
    assert m.full_path("app/api/routes/articles/articles_resource.py", line_of(art, "@router.get")) == "/api/articles/{slug}"


def test_express_routers_chained_and_exported_get_their_mount(tmp_path):
    r = Reader(write(tmp_path, EXPRESS), {"surfaces": []}, "T", None, source_files(str(tmp_path)))
    m = Mounts(r.outline)
    ctl = EXPRESS["src/routes/article.controller.ts"]
    assert m.full_path("src/routes/article.controller.ts", line_of(ctl, "router.get(")) == "/api/articles/feed"
    assert m.full_path("src/routes/article.controller.ts", line_of(ctl, "router.post(")) == "/api/articles"
    # a route on the application itself is under no mount: left to the map
    assert m.full_path("src/main.ts", line_of(EXPRESS["src/main.ts"], "app.get(")) is None


def test_a_route_decorator_names_the_function_under_it(tmp_path):
    r = Reader(write(tmp_path, FASTAPI), {"surfaces": []}, "T", None, source_files(str(tmp_path)))
    users = FASTAPI["app/api/routes/users.py"]
    assert r.decorated_fn("app/api/routes/users.py", line_of(users, "@router.post(")).name == "register"
    assert r.decorated_fn("app/api/routes/users.py", line_of(users, "status_code=201")).name == "register"
    assert r.decorated_fn("app/api/routes/users.py", line_of(users, '"/login"')).name == "login"
    assert r.decorated_fn("app/api/routes/users.py", line_of(users, "router = APIRouter()")) is None


def test_settings_classes_are_read_from_the_environment(tmp_path):
    r = Reader(write(tmp_path, FASTAPI), {"surfaces": []}, "T", None, source_files(str(tmp_path)))
    text = FASTAPI["app/core/settings.py"]
    assert r.in_settings_class("app/core/settings.py", line_of(text, "api_prefix"))  # through BaseAppSettings
    assert r.in_settings_class("app/core/settings.py", line_of(text, "debug"))
    assert not r.in_settings_class("app/core/settings.py", line_of(text, "return AppSettings()"))


def test_an_object_built_for_one_call_is_no_component(tmp_path):
    r = Reader(write(tmp_path, FASTAPI), {"surfaces": []}, "T", None, source_files(str(tmp_path)))
    assert r.built_per_call("SignupValidator", "app/services/validation.py")
    assert not r.built_per_call("AppSettings", "app/core/settings.py")  # not named as a per-call object
