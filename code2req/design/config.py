"""Configuration files: the values settings take where the code reads them with no default (Spring's
application.properties, .NET's appsettings.json, .env.example, config.yaml), and the database they point at
(a datasource URL, a docker-compose image). A secret's value is never copied."""

from __future__ import annotations

import json
import os
import re

CONFIG_FILE = re.compile(
    r"(^|/)(application|bootstrap)(-[\w.]+)?\.(properties|ya?ml)$"  # Spring
    r"|(^|/)appsettings(\.[\w]+)?\.json$"  # .NET
    r"|(^|/)(\.env\.(example|sample|template|dist|defaults)|[\w-]+\.env)$"  # app.env, example.env; never .env itself
    r"|(^|/)(config|settings)(\.[\w]+)?\.(ya?ml|json|toml)$|(^|/)config/[\w.-]+\.(ya?ml|json|toml|properties)$"
    r"|(^|/)(docker-)?compose[\w.-]*\.ya?ml$")
# a secret's name: TOKEN_SYMMETRIC_KEY, jwt.secret, DB_PASSWORD, GITHUB_TOKEN; not ACCESS_TOKEN_DURATION
SECRET = re.compile(r"(?i)secret|passw|credential|salt|private[_.-]?key|(token|key|pwd)\}?$")
# the database a value points at: (pattern, actor name as reader.norm_actor writes it)
ENGINES = [(re.compile(r"(?i)jdbc:sqlite|\bsqlite3?:|sqlite://|\.sqlite3?\b"), "Sqlite"),
           (re.compile(r"(?i)jdbc:mariadb|mariadb://|image:\s*['\"]?mariadb"), "MariaDb"),
           (re.compile(r"(?i)jdbc:mysql|mysql(\+\w+)?://|image:\s*['\"]?mysql"), "MySql"),
           (re.compile(r"(?i)jdbc:postgresql|postgres(ql)?(\+\w+)?://|image:\s*['\"]?(postgres|postgis)"), "PostgreSql"),
           (re.compile(r"(?i)jdbc:sqlserver|image:\s*['\"]?mcr\.microsoft\.com/mssql"), "SqlServer"),
           (re.compile(r"(?i)jdbc:oracle"), "Oracle"),
           (re.compile(r"(?i)jdbc:h2:|\bh2:mem"), "H2"),
           (re.compile(r"(?i)mongodb(\+srv)?://|image:\s*['\"]?mongo\b"), "MongoDb")]
# the database a driver the code imports talks to: gorm.io/driver/sqlite, github.com/lib/pq, psycopg, mysql2, mongoose
DRIVERS = [(re.compile(r"(?i)sqlite"), "Sqlite"), (re.compile(r"(?i)mariadb"), "MariaDb"),
           (re.compile(r"(?i)mysql"), "MySql"),
           (re.compile(r"(?i)postgres|/pq\b|\bpgx\b|psycopg|\bpg\b|asyncpg|npgsql"), "PostgreSql"),
           (re.compile(r"(?i)sqlserver|mssql|SqlClient"), "SqlServer"), (re.compile(r"(?i)mongo"), "MongoDb")]


def drivers(imports: list[str]) -> list[str]:
    """The databases the imported drivers talk to."""
    text = "\n".join(imports)
    return [name for rx, name in DRIVERS if rx.search(text)]


SQL = {"Sqlite", "MariaDb", "MySql", "PostgreSql", "SqlServer", "Oracle", "H2", "Database"}


def key_of(name: str) -> str:
    """One spelling for a setting's name wherever it is written: ${jwt.sessionTime:3600}, JWT_SESSION_TIME,
    Jwt:SessionTime and jwt.session-time are all jwtsessiontime."""
    s = str(name).strip()
    m = re.fullmatch(r"\$\{([^}:]+)(?::-?[^}]*)?\}", s)  # ${jwt.secret}, ${PORT:8080}, ${VAR:-x}: the name only
    return re.sub(r"[^a-z0-9]", "", (m.group(1) if m else s).lower())


def _flat(prefix: str, v, out: dict) -> None:
    if isinstance(v, dict):
        for k, x in v.items():
            _flat(f"{prefix}.{k}" if prefix else str(k), x, out)
    elif not isinstance(v, list) and prefix:
        out[prefix] = "" if v is None else str(v).lower() if isinstance(v, bool) else str(v)


def read_file(path: str) -> dict[str, str]:
    """A config file's settings, flattened: {'jwt.sessionTime': '86400', 'ConnectionStrings.Db': '...'}."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return {}
    out: dict[str, str] = {}
    low = path.lower()
    try:
        if low.endswith((".yml", ".yaml")):
            import yaml
            for doc in yaml.safe_load_all(text):
                _flat("", doc, out)
        elif low.endswith(".json"):
            _flat("", json.loads(re.sub(r"^\s*//.*$", "", text, flags=re.M)), out)  # appsettings may carry // notes
        elif low.endswith(".toml"):
            import tomllib
            _flat("", tomllib.loads(text), out)
        else:  # .properties, .env: key=value or key: value
            for line in text.splitlines():
                m = re.match(r"\s*(?:export\s+)?([\w.\-\[\]]+)\s*[=:]\s*(.*?)\s*$", line)
                if m and not line.lstrip().startswith(("#", "!")):
                    out[m.group(1)] = m.group(2).strip("'\"")
    except Exception:  # noqa: BLE001 - a file that does not parse gives nothing
        return out
    return out


class Config:
    """The repo's configuration files, read once."""

    def __init__(self, repo: str, files: list[str]):
        self.files = [f for f in files if CONFIG_FILE.search(f.replace("\\", "/")) and not re.search(
            r"(^|/)(test|tests|src/test|__tests__|testdata|examples?)/", f.replace("\\", "/"))]
        self.values: dict[str, tuple[str, str, str]] = {}  # key_of(name) -> (name, value, file)
        self.text = ""
        for rel in sorted(self.files, key=lambda f: (re.search(r"-(test|dev|local)\b", f) is not None, f)):
            path = os.path.join(repo, rel)
            for name, value in read_file(path).items():
                self.values.setdefault(key_of(name), (name, value, rel))
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    self.text += f.read() + "\n"
            except OSError:
                pass

    def value(self, setting: str) -> dict | None:
        """Where the configuration sets a setting the code reads: {name, value, file, secret}. A secret's value is
        not given."""
        hit = self.values.get(key_of(setting))
        if hit is None:
            return None
        name, value, rel = hit
        secret = bool(SECRET.search(name) or SECRET.search(setting))
        value = re.sub(r"(//[^/:@\s]+:)[^@/\s]+@", r"\1***@", value)  # a password inside a URL
        return {"name": name, "value": "" if secret else value, "file": rel, "secret": secret}

    def engines(self) -> list[str]:
        """The databases the configuration points at, in the order of ENGINES."""
        return [name for rx, name in ENGINES if rx.search(self.text)]
