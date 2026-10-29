"""Properties of .github/workflows/ci.yml that must not regress. GitHub cannot be asked from here whether the workflow
runs, but its security-relevant and reliability-relevant structure can be pinned, and its commands are the ones
the tests below and the README use locally."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
TEXT = WORKFLOW_PATH.read_text(encoding="utf-8")
WORKFLOW = yaml.safe_load(TEXT)
JOBS = WORKFLOW["jobs"]
TRIGGERS = WORKFLOW.get("on", WORKFLOW.get(True))  # YAML 1.1 reads a bare `on` as the boolean True
SHA = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def steps(job):
    return JOBS[job]["steps"]


def all_steps():
    return [(name, step) for name, job in JOBS.items() for step in job["steps"]]


def run_text(job):
    return "\n".join(step.get("run", "") for step in steps(job))


# --- triggers and permissions ---


GUARD = "github.event_name != 'push' || github.ref_name == github.event.repository.default_branch"


def test_it_runs_for_pushes_pull_requests_and_manual_dispatch():
    assert set(TRIGGERS) == {"push", "pull_request", "workflow_dispatch"}


def test_no_branch_name_is_written_into_the_workflow():
    assert not (TRIGGERS["push"] or {}).get("branches")  # the guard on each job selects the default branch
    assert not (TRIGGERS["pull_request"] or {}).get("branches")
    assert not re.search(r"(main|master)", re.sub(r"#.*", "", TEXT))


@pytest.mark.parametrize("job", sorted(JOBS))
def test_every_job_runs_on_a_push_only_to_the_default_branch(job):
    assert JOBS[job]["if"] == GUARD


def test_permissions_are_read_only_and_never_widened_by_a_job():
    assert WORKFLOW["permissions"] == {"contents": "read"}
    assert all("permissions" not in job for job in JOBS.values())


def test_there_are_no_privileged_triggers():
    assert not {"pull_request_target", "workflow_run", "schedule", "issue_comment", "release"} & set(TRIGGERS)


def test_a_superseded_pull_request_run_is_cancelled_but_default_branch_runs_never_are():
    concurrency = WORKFLOW["concurrency"]

    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert "github.ref" in concurrency["group"] and "github.workflow" in concurrency["group"]


# --- supply chain ---


def test_every_third_party_action_is_pinned_to_a_full_commit_sha_with_a_version_comment():
    references = [step["uses"] for _, step in all_steps() if "uses" in step]
    lines = [line for line in TEXT.splitlines() if "uses:" in line]

    assert references and len(lines) == len(references)
    for reference in references:
        assert SHA.match(reference), reference
    assert all(re.search(r"# v\d+(\.\d+)*$", line) for line in lines)


def test_only_the_two_expected_actions_are_used():
    used = {step["uses"].split("@")[0] for _, step in all_steps() if "uses" in step}

    assert used == {"actions/checkout", "actions/setup-python"}


def test_checkout_does_not_leave_credentials_in_the_workspace():
    for _, step in all_steps():
        if step.get("uses", "").startswith("actions/checkout"):
            assert step["with"]["persist-credentials"] is False


def test_runners_are_pinned_rather_than_floating():
    assert {job["runs-on"] for job in JOBS.values()} == {"ubuntu-24.04"}


def test_nothing_is_deployed_published_or_secret_dependent():
    assert "secrets." not in TEXT
    assert not re.search(r"docker (push|login)|twine|pip upload|gh release|git push|ssh |scp |kubectl|helm ", TEXT)
    assert "id-token" not in TEXT


def test_the_postgresql_service_image_matches_the_one_in_the_compose_file():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    image = JOBS["postgres-tests"]["services"]["postgres"]["image"]

    assert re.search(r"@sha256:[0-9a-f]{64}$", image)
    assert image == compose["services"]["db"]["image"]


# --- reliability ---


def test_every_job_has_a_timeout():
    for name, job in JOBS.items():
        assert 0 < job["timeout-minutes"] <= 30, name


def test_dependencies_are_installed_with_the_existing_pins_and_checked():
    for job in ("lint-and-test", "postgres-tests"):
        text = run_text(job)

        assert "pip install -r requirements-dev.txt -c constraints.txt" in text
        assert "pip check" in text


def test_pip_caching_is_keyed_on_the_dependency_files():
    for job in ("lint-and-test", "postgres-tests"):
        [setup] = [s for s in steps(job) if s.get("uses", "").startswith("actions/setup-python")]

        assert setup["with"]["cache"] == "pip"
        assert set(setup["with"]["cache-dependency-path"].split()) == {
            "requirements.txt",
            "requirements-dev.txt",
            "constraints.txt",
        }


def test_ruff_lint_and_format_are_both_checked():
    text = run_text("lint-and-test")

    assert "ruff check" in text and "ruff format --check" in text


def test_the_tests_run_on_the_python_of_the_image_and_the_one_used_for_development():
    matrix = JOBS["lint-and-test"]["strategy"]["matrix"]["python-version"]
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert matrix == ["3.13", "3.14"]
    assert "python:3.13-slim" in dockerfile  # the image is tested on the version it ships
    assert JOBS["lint-and-test"]["strategy"]["fail-fast"] is False


def test_the_fast_job_allows_only_database_skips():
    text = run_text("lint-and-test")

    assert "grep '^SKIPPED'" in text and "grep -v 'TEST_POSTGRES_DB'" in text
    assert "TEST_POSTGRES_DB" not in str(JOBS["lint-and-test"].get("env", {}))  # so those tests really do skip


def test_the_database_job_forbids_any_skip():
    text = run_text("postgres-tests")

    assert "grep -E '^SKIPPED|[0-9]+ skipped'" in text  # both the per-test lines and the summary count
    assert "exit 1" in text
    assert "TEST_POSTGRES_DB" in JOBS["postgres-tests"]["env"]  # a database is provided, so a skip means a mistake


def test_the_database_is_waited_for_by_a_health_check_and_by_the_job():
    service = JOBS["postgres-tests"]["services"]["postgres"]
    text = run_text("postgres-tests")

    assert "--health-cmd" in service["options"] and "--health-retries" in service["options"]
    assert "psycopg.connect" in text and "time.monotonic() > deadline" in text  # explicit readiness loop with a limit
    wait = next(i for i, s in enumerate(steps("postgres-tests")) if "Wait for PostgreSQL" in s.get("name", ""))
    tests = next(i for i, s in enumerate(steps("postgres-tests")) if s.get("name") == "Tests with PostgreSQL")
    assert wait < tests


def test_the_ci_databases_are_disposable_and_satisfy_the_test_fixtures_guards():
    env = JOBS["postgres-tests"]["env"]

    assert env["TEST_POSTGRES_DB"].endswith("_test")
    assert env["TEST_POSTGRES_DB"] != env["POSTGRES_DB"]
    assert env["POSTGRES_HOST"] == "127.0.0.1"


def test_credentials_in_the_workflow_are_obvious_throwaway_values():
    for job in JOBS.values():
        for name, value in {**job.get("env", {}), **job.get("services", {}).get("postgres", {}).get("env", {})}.items():
            if "PASSWORD" in name or "SECRET" in name:
                assert value.startswith("ci") or value.startswith("ci-"), (name, value)


def test_the_docker_job_builds_the_image_and_smoke_tests_the_production_stack():
    text = run_text("docker")

    assert "docker build" in text and "docker compose -f docker-compose.yml up -d --build --wait" in text
    assert "/health/db" in text and "/docs" in text and "/auth/register" in text
    assert text.index("up -d --build --wait") < text.index("/health/db")


def test_the_docker_job_always_cleans_up_and_shows_logs_on_failure():
    cleanup = [s for s in steps("docker") if "down --volumes" in s.get("run", "")]
    logs = [s for s in steps("docker") if "logs" in s.get("run", "")]

    assert cleanup and cleanup[0]["if"] == "always()"
    assert logs and logs[0]["if"] == "failure()"


@pytest.mark.parametrize("job", sorted(JOBS))
def test_commands_run_under_bash_with_the_default_pipefail_behavior(job):
    assert (
        WORKFLOW["defaults"]["run"]["shell"] == "bash"
    )  # bash -e -o pipefail: a failing pytest in `| tee` fails the step


# --- the file itself ------------------------------------------------------------------------------------------------


class StrictLoader(yaml.SafeLoader):
    """A loader that refuses duplicate keys, which YAML (and PyYAML) would otherwise resolve silently."""


def _no_duplicates(loader, node, deep=False):
    keys = [loader.construct_object(key, deep=deep) for key, _ in node.value]
    duplicates = {key for key in keys if keys.count(key) > 1}
    if duplicates:
        raise yaml.constructor.ConstructorError(None, None, f"duplicate keys: {sorted(map(str, duplicates))}")
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


def test_the_workflow_has_no_duplicate_keys_and_a_name():
    assert yaml.load(TEXT, Loader=StrictLoader) == WORKFLOW
    assert WORKFLOW["name"] == "CI"


def test_the_file_uses_lf_line_endings_and_spaces_only():
    raw = WORKFLOW_PATH.read_bytes()

    assert b"\r" not in raw and b"\t" not in raw and raw.endswith(b"\n")


EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.S)
ALLOWED_CONTEXT_VALUES = {
    "matrix.python-version",
    "github.workflow",
    "github.ref",
}


def test_expressions_use_only_known_safe_contexts():
    used = {expression.strip() for expression in EXPRESSION.findall(TEXT)}

    assert used == ALLOWED_CONTEXT_VALUES | {"github.event_name == 'pull_request'"}
    assert "${{" not in "\n".join(run_text(job) for job in JOBS)  # never inside a shell script (no script injection)


def test_no_untrusted_event_data_reaches_a_shell_command():
    for name in ("head_ref", "pull_request.title", "pull_request.body", "issue", "comment", "commits", "head_commit"):
        assert name not in TEXT, name


def test_the_if_expressions_reference_only_trusted_values():
    for job in JOBS.values():
        assert set(re.findall(r"github\.[a-z_.]+", job["if"])) == {
            "github.event_name",
            "github.ref_name",
            "github.event.repository.default_branch",
        }
