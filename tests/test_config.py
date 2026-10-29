import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import make_url

from app import database
from app.config import Settings, get_settings

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
        if not key.startswith("POSTGRES_")
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
