"""The container setup: properties of the Dockerfile and Compose files that must not regress, and the switch that
turns the interactive documentation off. Nothing here needs Docker; the image itself is verified by building it."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from app.config import ServerSettings

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
OVERRIDE = yaml.safe_load((ROOT / "docker-compose.override.yml").read_text(encoding="utf-8"))
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}")


def dockerfile_instructions():
    """Each instruction as one string, with backslash continuations joined and comments dropped."""
    joined, current = [], ""
    for raw in DOCKERFILE.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        current = f"{current} {line.rstrip(chr(92)).strip()}".strip()
        if not line.endswith("\\"):
            joined.append(current)
            current = ""
    return joined


def instructions(name):
    return [
        instruction.split(None, 1)[1].strip()
        for instruction in dockerfile_instructions()
        if instruction.split(None, 1)[0].upper() == name
    ]


# --- the Dockerfile --------------------------------------------------------------------------------------------------


def test_every_base_image_is_pinned_by_digest():
    images = [line.split()[0] for line in instructions("FROM")]

    assert len(images) == 2  # a build stage and the final stage
    assert all(DIGEST.search(image) for image in images)
    assert all(image.startswith("python:3.13-slim@") for image in images)


def test_the_final_image_runs_as_a_numeric_non_root_user():
    [user] = instructions("USER")

    assert re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", user)
    assert DOCKERFILE.index("USER ") > DOCKERFILE.index(
        "COPY app"
    )  # code is copied (as root) before dropping privileges


def test_the_application_account_has_no_shell_or_home():
    assert "--no-create-home" in DOCKERFILE and "/usr/sbin/nologin" in DOCKERFILE


def test_only_the_runtime_files_are_copied_and_no_secret_or_dev_file_is():
    sources = set()
    for step in instructions("COPY"):
        arguments = [word for word in step.split() if not word.startswith("--")]
        sources.update(arguments[:-1])  # everything but the destination

    assert sources == {"requirements.txt", "constraints.txt", "/opt/venv", "app", "alembic", "alembic.ini"}
    assert not instructions("ADD")


def test_only_runtime_requirements_are_installed_with_every_version_constrained():
    [install] = [step for step in instructions("RUN") if "pip install" in step]

    assert "-r requirements.txt" in install and "-c constraints.txt" in install
    assert "requirements-dev" not in DOCKERFILE


def test_no_secret_is_baked_into_the_image():
    assert not re.search(r"^\s*(ENV|ARG)\s+\S*(SECRET|PASSWORD|TOKEN|KEY)\S*", DOCKERFILE, re.M | re.I)
    assert "SECRET_KEY" not in DOCKERFILE and "POSTGRES_PASSWORD" not in DOCKERFILE


def test_the_server_starts_in_exec_form_without_trusting_forwarded_headers():
    [command] = instructions("CMD")

    assert command.startswith('["uvicorn"')  # exec form: uvicorn is PID 1 and gets SIGTERM directly
    assert '"--no-proxy-headers"' in command
    for forbidden in ("--proxy-headers", "--forwarded-allow-ips", "--reload", "--workers"):
        assert f'"{forbidden}"' not in command
    assert '"--timeout-graceful-shutdown"' in command


def test_the_healthcheck_reports_database_readiness_without_extra_tools():
    [check] = instructions("HEALTHCHECK")

    assert "/health/db" in check and "urllib" in check
    assert "curl" not in check and "wget" not in check


def test_python_does_not_write_bytecode_into_the_read_only_image():
    assert "PYTHONDONTWRITEBYTECODE=1" in DOCKERFILE


# --- .dockerignore ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry", [".env", ".env.*", ".git", ".venv", "tests", "__pycache__", ".pytest_cache", "requirements-dev.txt"]
)
def test_dockerignore_keeps_secrets_and_development_files_out_of_the_build_context(entry):
    assert entry in DOCKERIGNORE


def test_dockerignore_does_not_exclude_what_the_image_needs():
    for needed in ("app", "alembic", "alembic.ini", "requirements.txt", "constraints.txt"):
        assert needed not in DOCKERIGNORE


# --- docker-compose.yml (production) ---


SERVICES = COMPOSE["services"]


def test_the_project_has_a_fixed_name():
    assert COMPOSE["name"] == "task-manager-api"


def test_postgresql_is_not_published_in_the_production_file():
    assert "ports" not in SERVICES["db"]


def test_the_api_is_published_on_loopback_only():
    [port] = SERVICES["api"]["ports"]

    assert port.startswith("127.0.0.1:")
    assert port.endswith(":8000")


def test_images_are_pinned_and_the_database_image_by_digest():
    assert DIGEST.search(SERVICES["db"]["image"])


@pytest.mark.parametrize("service", ["api", "migrate"])
def test_application_containers_are_locked_down(service):
    config = SERVICES[service]

    assert config["read_only"] is True
    assert config["cap_drop"] == ["ALL"] and "cap_add" not in config
    assert "no-new-privileges:true" in config["security_opt"]
    assert config["pids_limit"] > 0


def test_the_database_container_keeps_only_the_capabilities_it_needs():
    db = SERVICES["db"]

    assert db["cap_drop"] == ["ALL"]
    assert set(db["cap_add"]) == {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"}
    assert "no-new-privileges:true" in db["security_opt"]


def test_registration_and_docs_are_off_by_default_in_production():
    environment = SERVICES["api"]["environment"]

    assert environment["REGISTRATION_ENABLED"] == "${REGISTRATION_ENABLED:-false}"
    assert environment["DOCS_ENABLED"] == "${DOCS_ENABLED:-false}"


def test_trusted_proxies_default_to_none_and_the_container_connects_to_the_db_service():
    environment = SERVICES["api"]["environment"]

    assert environment["TRUSTED_PROXIES"] == "${TRUSTED_PROXIES:-}"
    assert (environment["POSTGRES_HOST"], environment["POSTGRES_PORT"]) == ("db", "5432")


def test_secrets_are_required_variables_never_literal_values():
    for service in ("db", "migrate", "api"):
        for name, value in SERVICES[service]["environment"].items():
            if "PASSWORD" in name or "SECRET" in name:
                assert re.fullmatch(r"\$\{[A-Z_]+:\?[^}]+\}", value), (service, name)


def test_the_migration_does_not_receive_the_signing_key():
    assert "SECRET_KEY" not in SERVICES["migrate"]["environment"]


def test_start_up_order_is_database_then_migration_then_api():
    assert SERVICES["migrate"]["depends_on"]["db"]["condition"] == "service_healthy"
    assert SERVICES["api"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert SERVICES["migrate"]["command"] == ["alembic", "upgrade", "head"]


def test_restart_behavior():
    assert SERVICES["db"]["restart"] == SERVICES["api"]["restart"] == "unless-stopped"
    assert SERVICES["migrate"]["restart"] == "no"


def test_the_database_keeps_a_named_volume():
    assert SERVICES["db"]["volumes"] == ["postgres_data:/var/lib/postgresql/data"]
    assert "postgres_data" in COMPOSE["volumes"]


# --- docker-compose.override.yml (development) ---


def test_the_override_publishes_the_database_on_loopback_for_local_development():
    [port] = OVERRIDE["services"]["db"]["ports"]

    assert port.startswith("127.0.0.1:") and port.endswith(":5432")


def test_the_containerized_api_is_opt_in_in_development_so_up_db_still_starts_only_the_database():
    assert OVERRIDE["services"]["api"]["profiles"] == ["app"]
    assert OVERRIDE["services"]["migrate"]["profiles"] == ["app"]


def test_the_override_loosens_only_registration_and_docs():
    api = OVERRIDE["services"]["api"]

    assert set(api) == {"profiles", "environment"}
    assert set(api["environment"]) == {"REGISTRATION_ENABLED", "DOCS_ENABLED"}
    assert set(OVERRIDE["services"]["db"]) == {"ports"}


# --- dependency pins for the image ---


def test_every_runtime_requirement_and_the_linux_only_extra_is_pinned():
    pinned = {
        re.split(r"[=;\s]", line, maxsplit=1)[0].lower().replace("_", "-")
        for line in (ROOT / "constraints.txt").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }

    assert "uvloop" in pinned  # installed by uvicorn[standard] on Linux only
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        name = re.split(r"[=\[]", line, maxsplit=1)[0].lower().replace("_", "-")
        assert name in pinned, name
    assert "uvloop==" in (ROOT / "constraints.txt").read_text(encoding="utf-8")
    assert 'sys_platform != "win32"' in (ROOT / "constraints.txt").read_text(encoding="utf-8")


# --- the documentation switch ---


def fetch_paths(**env):
    code = (
        f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
        "from fastapi.testclient import TestClient; from app.main import app; c = TestClient(app); "
        "print(*[c.get(p).status_code for p in ('/docs', '/redoc', '/openapi.json', '/health')])"
    )
    environment = {**{k: v for k, v in os.environ.items() if k != "DOCS_ENABLED"}, **env}
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, env=environment, cwd=ROOT
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.split()


def test_the_documentation_is_available_by_default():
    assert ServerSettings(_env_file=None).docs_enabled is True
    assert fetch_paths() == ["200", "200", "200", "200"]


@pytest.mark.parametrize("value", ["false", "False", "0", "no", "off"])
def test_the_documentation_can_be_switched_off_and_the_api_keeps_working(value):
    assert fetch_paths(DOCS_ENABLED=value) == ["404", "404", "404", "200"]


def test_an_invalid_docs_setting_is_rejected(monkeypatch):
    from pydantic import ValidationError

    monkeypatch.setenv("DOCS_ENABLED", "maybe")

    with pytest.raises(ValidationError):
        ServerSettings(_env_file=None)


# --- documented settings ---


def test_the_new_settings_are_documented_in_env_example_without_real_values():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "DOCS_ENABLED" in text and "API_PORT" in text
    assert not re.search(r"^(SECRET_KEY|POSTGRES_PASSWORD)=(?!<)", text, re.M)  # only placeholders
