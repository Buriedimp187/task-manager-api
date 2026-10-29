# Security policy

This is a small, single-maintainer project. The policy below says what is covered and what a report should contain.
It makes no promises about response times and there is no bug bounty.

## Reporting a vulnerability

Please report vulnerabilities privately, using GitHub's **private vulnerability reporting**. It is enabled for this
repository. Open the repository's **Security** tab, choose **Report a vulnerability**, and fill in the form (see
"What to include" below). The report is visible only to you and the maintainer until the maintainer decides otherwise.

**Do not publish details of a vulnerability** in a public issue, pull request, discussion or commit. If you cannot use
GitHub's form, open an issue that says only that you have a security report and asks for a private way to send it. Include
no technical details, payloads, tokens or personal data.

No other private contact (such as an e-mail address) is offered. A reporter needs a GitHub account to use the form.

## What to include

A good report lets the maintainer reproduce and judge the problem without guessing:

- What is affected: the endpoint, setting, file or Docker/Compose/CI configuration involved.
- The version: the commit hash (`git rev-parse HEAD`) and how you ran it (local `uvicorn`, the Docker stack,
  which Compose files, Python version).
- Steps to reproduce, with the smallest request or configuration that shows the problem.
- The impact you observed or expect: for example, reading another user's tasks, bypassing a rate limit, or
  forging a token.
- Any relevant, **redacted** configuration. Never include real passwords, `SECRET_KEY` values, tokens or other
  people's data in a report. Use throwaway values.
- A suggested fix, if you have one (optional).

## Scope

In scope:

- The application code in `app/`, including authentication, token validation, ownership checks, rate limiting,
  request-size limits and the database access.
- The database migrations in `alembic/`.
- The `Dockerfile`, the Compose files and the GitHub Actions workflow, where they weaken the security of a
  deployment.

Out of scope:

- Problems that need an already compromised host, database, `.env` file or `SECRET_KEY`.
- Findings against third-party software itself (FastAPI, PostgreSQL, Docker and so on). Report those upstream.
  A vulnerable pinned version of a dependency is in scope, since the pins live here.
- Denial of service by sheer traffic volume, and anything that needs scanning or attacking infrastructure you do
  not own.
- Behavior that the README already lists under its known limitations. These are known trade-offs, not hidden
  flaws, although a report that shows a worse impact than documented is welcome. At the time of writing they
  include: rate-limit counters are kept in memory per process, tokens cannot be revoked before they expire, and
  there is no password reset, email verification or account deletion.

## Supported versions

There are no releases or tags yet. Only the latest commit on the default branch is supported, and fixes are made
there; nothing is backported. If releases are published later, this section will say which ones are supported.

## What a reporter is asked to do

- Test only against a deployment you own, or against a local copy.
- Do not access, change or delete data that is not yours. If you reach other people's data by accident, stop,
  do not keep or share it, and say so in the report.
- Do not run tests that degrade a service other people use.
- Give the maintainer a reasonable chance to fix the problem before you disclose it, and agree on timing with
  them. Because this is a one-person project, no fixed time limit is promised in either direction.

## What the maintainer will try to do

On a best-effort basis: acknowledge the report, confirm or explain why it is not a
vulnerability, fix confirmed problems, and credit the reporter if they wish. No response time is guaranteed.

## Repository protections

These GitHub settings were enabled and checked on 2026-10-29. They are settings of the hosted repository, not part of the
code, so they can change without a commit.

- Private vulnerability reporting, Dependabot alerts and Dependabot security updates.
- Secret scanning and push protection. The optional extra patterns and validity checks are off.
- A ruleset on `main` that blocks deleting the branch and force-pushes.
- GitHub Actions workflows run with a read-only token, actions must be pinned to full commit SHAs, and workflows from
  outside contributors need the maintainer's approval before they run.

Not enabled: required pull requests, required reviews, required status checks, and code scanning (CodeQL). The maintainer
can push to `main` directly, and nobody else reviews changes before they land. Dependabot version updates are not
configured, so pinned dependencies are not refreshed automatically.
