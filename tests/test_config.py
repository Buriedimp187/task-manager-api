import os
import subprocess
import sys
from pathlib import Path

import pytest
import re

from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import make_url

from app import database
from app.config import MIN_SECRET_KEY_LENGTH, AuthSettings, Settings, get_auth_settings, get_settings
from app.main import app

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SPECIAL_PASSWORD = "p@ss:w/ord#?%&=+ ü"


@pytest.fixture
def db_env(monkeypatch):
    monkeypatch.setenv("POSTGRES_USER", "app_user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "test-only-value")
    monkeypatch.setenv("POSTGRES_DB", "app_db")
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    monkeypatch.delenv("POSTGRES_PORT", raising=False)


def test_settings_use_defaults_for_host_and_port(db_env):
    settings = Settings(_env_file=None)

    assert settings.postgres_host == "127.0.0.1"
    assert settings.postgres_port == 5432


def test_settings_read_host_and_port_from_environment(db_env, monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "db")
    monkeypatch.setenv("POSTGRES_PORT", "5433")

    settings = Settings(_env_file=None)

    assert settings.postgres_host == "db"
    assert settings.postgres_port == 5433


@pytest.mark.parametrize("missing", ["POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"])
def test_settings_require_credentials(db_env, monkeypatch, missing):
    monkeypatch.delenv(missing)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_validation_errors_do_not_echo_the_password(db_env, monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", SPECIAL_PASSWORD)
    monkeypatch.delenv("POSTGRES_DB")

    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None)

    assert "postgres_db" in str(error.value)
    assert SPECIAL_PASSWORD not in str(error.value)


@pytest.mark.parametrize(
    "line, expected",
    [
        ("POSTGRES_PASSWORD=Abc123xyz", "Abc123xyz"),
        ("POSTGRES_PASSWORD=pa#ss", "pa#ss"),
        ("POSTGRES_PASSWORD='pa$word1'", "pa$word1"),
        ("POSTGRES_PASSWORD='p@ss:w/ord#?%&=+'", "p@ss:w/ord#?%&=+"),
    ],
)
def test_settings_read_special_characters_from_an_env_file(db_env, monkeypatch, tmp_path, line, expected):
    monkeypatch.delenv("POSTGRES_PASSWORD")
    env_file = tmp_path / "test.env"
    env_file.write_text(f"POSTGRES_USER=app_user\n{line}\nPOSTGRES_DB=app_db\n", encoding="utf-8")

    assert Settings(_env_file=env_file).postgres_password.get_secret_value() == expected


def test_database_url_uses_the_psycopg_driver(db_env):
    url = Settings(_env_file=None).database_url

    assert url.drivername == "postgresql+psycopg"
    assert (url.username, url.host, url.port, url.database) == (
        "app_user",
        "127.0.0.1",
        5432,
        "app_db",
    )


def test_database_url_handles_special_characters_in_password(db_env, monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", SPECIAL_PASSWORD)

    url = Settings(_env_file=None).database_url
    rendered = url.render_as_string(hide_password=False)

    assert url.password == SPECIAL_PASSWORD
    assert SPECIAL_PASSWORD not in rendered  # reserved characters are escaped
    assert make_url(rendered).password == SPECIAL_PASSWORD  # and survive a round trip


def test_password_is_not_exposed_in_reprs(db_env, monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", SPECIAL_PASSWORD)
    settings = Settings(_env_file=None)

    assert SPECIAL_PASSWORD not in repr(settings)
    assert SPECIAL_PASSWORD not in str(settings)
    assert SPECIAL_PASSWORD not in str(settings.database_url)  # SQLAlchemy masks it


def test_engine_is_configured_without_connecting(db_env):
    get_settings.cache_clear()
    database.get_engine.cache_clear()
    try:
        engine = database.get_engine()

        assert engine.url.drivername == "postgresql+psycopg"
        assert engine.pool._pre_ping is True
    finally:
        database.get_engine.cache_clear()
        get_settings.cache_clear()


def test_importing_the_app_needs_no_configuration_or_database(tmp_path):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("POSTGRES_") and key != "SECRET_KEY"
    }
    env["PYTHONPATH"] = str(PROJECT_ROOT)

    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=tmp_path,  # no .env file here
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


# --- The token-signing secret ---------------------------------------------------------------------

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def no_configuration(monkeypatch, tmp_path):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env file here
    get_auth_settings.cache_clear()
    yield monkeypatch
    get_auth_settings.cache_clear()


def test_the_secret_key_is_required(no_configuration):
    with pytest.raises(ValidationError) as error:
        AuthSettings(_env_file=None)

    assert "secret_key" in str(error.value)


@pytest.mark.parametrize("length", [0, 1, 16, MIN_SECRET_KEY_LENGTH - 1])
def test_a_short_secret_key_is_rejected_without_echoing_it(no_configuration, length):
    value = "Q" * length  # a character that the error message itself does not contain
    no_configuration.setenv("SECRET_KEY", value)

    with pytest.raises(ValidationError) as error:
        AuthSettings(_env_file=None)

    message = str(error.value)
    assert f"at least {MIN_SECRET_KEY_LENGTH} characters" in message
    assert "SECRET_KEY" in message and "token_urlsafe" in message  # says how to fix it
    if value:
        assert value not in message


@pytest.mark.parametrize("length", [MIN_SECRET_KEY_LENGTH, 64, 200])
def test_a_long_enough_secret_key_is_accepted_and_stays_hidden(no_configuration, length):
    value = "s" * length
    no_configuration.setenv("SECRET_KEY", value)

    settings = AuthSettings(_env_file=None)

    assert settings.secret_key.get_secret_value() == value
    assert value not in repr(settings) and value not in str(settings)


def test_the_secret_key_can_come_from_a_dotenv_file(no_configuration, tmp_path):
    env_file = tmp_path / "test.env"
    value = "d" * 40
    env_file.write_text(f"SECRET_KEY={value}\n", encoding="utf-8")

    assert AuthSettings(_env_file=env_file).secret_key.get_secret_value() == value


def test_the_placeholder_in_env_example_is_too_short_to_use(no_configuration):
    # Copying .env.example without editing it must not produce a working (and guessable) key.
    line = next(l for l in (REPO / ".env.example").read_text(encoding="utf-8").splitlines() if l.startswith("SECRET_KEY="))
    placeholder = line.split("=", 1)[1]
    no_configuration.setenv("SECRET_KEY", placeholder)

    assert placeholder.startswith("<") and placeholder.endswith(">")
    assert len(placeholder) < MIN_SECRET_KEY_LENGTH
    with pytest.raises(ValidationError):
        AuthSettings(_env_file=None)


def test_no_real_secret_is_committed_in_source_docs_or_examples():
    # A heuristic: any SECRET_KEY assignment with a long run of key-like characters.
    pattern = re.compile(r"(?i)secret_key\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{20,}")
    scanned = [*REPO.joinpath("app").rglob("*.py"), REPO / "README.md", REPO / ".env.example", REPO / "docker-compose.yml"]

    offenders = [path.name for path in scanned if pattern.search(path.read_text(encoding="utf-8"))]

    assert offenders == []


def test_the_database_settings_do_not_need_the_secret_key(no_configuration):
    # Alembic only loads the database settings, so migrations run without a signing key.
    no_configuration.setenv("POSTGRES_USER", "u")
    no_configuration.setenv("POSTGRES_PASSWORD", "p")
    no_configuration.setenv("POSTGRES_DB", "d")

    assert Settings(_env_file=None).postgres_user == "u"


def test_the_server_refuses_to_start_without_a_secret_key(no_configuration):
    with pytest.raises(ValidationError) as error:
        with TestClient(app):  # entering the client runs the startup code
            pass

    assert "secret_key" in str(error.value)


def test_the_server_refuses_to_start_with_a_short_secret_key(no_configuration):
    no_configuration.setenv("SECRET_KEY", "too-short-to-be-safe")

    with pytest.raises(ValidationError) as error:
        with TestClient(app):
            pass

    assert f"at least {MIN_SECRET_KEY_LENGTH} characters" in str(error.value)
    assert "too-short-to-be-safe" not in str(error.value)


def test_the_server_starts_with_a_valid_secret_key_and_no_database(no_configuration):
    no_configuration.setenv("SECRET_KEY", "v" * 48)

    with TestClient(app) as client:  # startup needs only the secret, not a database
        assert client.get("/health").json() == {"status": "ok"}
