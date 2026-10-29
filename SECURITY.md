# Security policy

> **Status: private vulnerability reporting is not available yet.** The repository is on GitHub but is still private, and
> no private contact has been set up, so this policy cannot yet tell you where to send a report. See
> "Reporting a vulnerability" below.

This is a small, single-maintainer project. The policy below says what is covered and what a report should contain.
It makes no promises about response times and there is no bug bounty.

## Reporting a vulnerability

> **A private reporting channel has not been configured yet.** This section must be completed by the maintainer
> when the project is made public (see "Before publishing" below). Until a private contact is listed here:
>
> - **Do not publish details of a vulnerability** in a public issue, pull request, discussion or commit.
> - If a public issue is the only way to reach the maintainer, open one that says only that you have a security
>   report and asks for a private way to send it. Include no technical details, payloads, tokens or personal data.

The intended channel is GitHub's **private vulnerability reporting**, which lets a reporter send details to the maintainer
without making them public. It is **not enabled**. GitHub does not appear to offer it while a repository is private (its
documentation describes it for public repositories), so the maintainer plans to turn it on in the repository's settings
once the repository is public. While the repository is private, people outside it cannot see it or report anything through
it. Until this section says the channel is enabled, treat it as incomplete rather than as permission to disclose publicly.

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

This applies once a private channel exists.

- Test only against a deployment you own, or against a local copy.
- Do not access, change or delete data that is not yours. If you reach other people's data by accident, stop,
  do not keep or share it, and say so in the report.
- Do not run tests that degrade a service other people use.
- Give the maintainer a reasonable chance to fix the problem before you disclose it, and agree on timing with
  them. Because this is a one-person project, no fixed time limit is promised in either direction.

## What the maintainer will try to do

Once a private channel exists, on a best-effort basis: acknowledge the report, confirm or explain why it is not a
vulnerability, fix confirmed problems, and credit the reporter if they wish. No response time is guaranteed.

## Before publishing

For the maintainer. Do these when the repository is made public, then delete this section:

- [ ] Enable private vulnerability reporting, or add a monitored private contact, and replace the notice under
      "Reporting a vulnerability" with it. While the repository was private, GitHub's API reported no state for this feature
      (HTTP 404), whereas it reports on or off for public repositories. That is evidence that the feature is not offered for
      a private repository, not proof, so expect to enable it right after the repository is made public, and check the
      settings. Do not list an address that nobody reads.
- [ ] Confirm that no real secret has ever been committed (`.env` is ignored, and `.env.example` holds only
      placeholders), and rotate any `SECRET_KEY` or database password that was ever shared.
- [x] Copyright holder decided: `LICENSE` names `Buriedimp187`, the owner of the project. No change needed.
