"""Configuration files (design/config.py): the values settings take, never a secret's, and the database the
configuration points at."""
from code2req.design.config import Config, key_of


def write(tmp_path, files):
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    return Config(str(tmp_path), list(files))


def test_a_setting_is_found_however_its_name_is_written():
    assert key_of("${jwt.sessionTime}") == key_of("JWT_SESSION_TIME") == key_of("Jwt:SessionTime") == key_of("jwt.session-time")
    assert key_of("${PORT:8080}") == "port" and key_of("${VAR:-x}") == "var"


def test_values_come_from_the_configuration_files_and_secrets_do_not(tmp_path):
    c = write(tmp_path, {
        "src/main/resources/application.properties": "spring.datasource.url=jdbc:sqlite:dev.db\njwt.secret=abc123\njwt.sessionTime=86400\n",
        "appsettings.json": '{\n  // the app\n  "Jwt": {"Issuer": "demo", "SigningKey": "k"}, "Paging": {"Max": 50}\n}',
        "app.env": "DB_SOURCE=postgresql://root:pw@localhost:5432/bank\nACCESS_TOKEN_DURATION=15m\n",
        "config/settings.yaml": "cache:\n  ttl: 300\n",
        "src/test/resources/application.properties": "jwt.sessionTime=1\n",  # tests' values are not the system's
    })
    assert c.value("${jwt.sessionTime}")["value"] == "86400"
    secret = c.value("${jwt.secret}")
    assert secret["secret"] and secret["value"] == ""
    assert c.value("Jwt:Issuer")["value"] == "demo" and c.value("Jwt:SigningKey")["value"] == ""
    assert c.value("Paging:Max")["value"] == "50" and c.value("CACHE_TTL")["value"] == "300"
    assert c.value("DB_SOURCE")["value"] == "postgresql://root:***@localhost:5432/bank"  # the password in the URL
    assert c.value("ACCESS_TOKEN_DURATION")["value"] == "15m"  # a token's duration is no secret
    assert c.engines() == ["Sqlite", "PostgreSql"]


def test_the_database_of_a_compose_file(tmp_path):
    c = write(tmp_path, {"docker-compose.yml": "services:\n  db:\n    image: postgres:16\n  cache:\n    image: redis:7\n"})
    assert c.engines() == ["PostgreSql"]


def test_the_database_of_the_driver_the_code_imports():
    from code2req.design.config import drivers
    assert drivers(['import (\n\t"gorm.io/driver/sqlite"\n\t"gorm.io/gorm"\n)']) == ["Sqlite"]
    assert drivers(['_ "github.com/lib/pq"']) == ["PostgreSql"] and drivers(["import mongoose from 'mongoose'"]) == ["MongoDb"]
