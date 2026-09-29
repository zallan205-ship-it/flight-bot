# AGENTS.md — Flight Bot AI Development Contract

## Project
Flight Bot is a personal Telegram bot that monitors Google Flights prices and alerts the user about relevant price changes.

## Non-negotiable constraints
- Runtime and development workflow must remain €0: no paid API, SaaS, hosted AI, or subscription is required.
- Never commit secrets, tokens, cookies, credentials, or local environment files.
- Preserve existing user-facing behavior unless a change is explicitly requested and tested.
- Do not add dependencies without a concrete justification.
- Do not introduce an LLM/AI service into the production monitoring loop.
- ML is allowed only after the data pipeline is stable and an offline evaluation dataset exists.

## Git workflow
- Never work directly on `main`.
- Development happens on a dedicated branch and reaches `main` through a reviewed pull request.
- Prefer small, logically isolated commits.
- A behavioral change requires tests.
- Do not mix refactoring, feature work, and unrelated formatting in one change.

## Architecture principles
- Keep scraper, domain logic, persistence, scheduling, alerts, and Telegram interface separated.
- Prefer deterministic, testable functions.
- Network calls must be mockable and must not be required by unit tests.
- Do not assume Google Flights HTML/API structure is stable; isolate and test parsing/consent behavior.
- Synchronous third-party operations must not silently block the async Telegram event loop; concurrency changes require explicit review.
- Database schema changes require a migration strategy (Alembic) before production schema evolution.

## Scraper-specific rules
- Google Flights is the current data source.
- `fast-flights` is the current parsing/query dependency.
- Round-trip handling currently uses two independent one-way searches because the observed round-trip payload did not expose the return leg reliably.
- Do not revert that design merely to simplify code without real-data evidence.
- Direct-flight filtering must be verified independently for outbound and return legs.
- Consent handling must be treated as a fragile integration boundary and covered by deterministic fixtures/tests.
- Do not infer that a price calculated from two one-way fares is always identical to Google's bundled round-trip fare.

## Testing
Minimum expected coverage for meaningful changes:
- unit tests for pure/domain logic;
- mocked scraper tests for parsing, direct-only filtering, consent handling, and one-way/round-trip composition;
- regression tests for every previously discovered scraper bug;
- integration/network tests are separate and must never be the only evidence of correctness.

## AI collaboration workflow
Use this lifecycle:
IDEA -> REQUIREMENT -> RESEARCH -> ARCHITECTURE -> IMPLEMENTATION -> TEST -> REVIEW -> MERGE

Roles:
- Lead/Architect: coordinates scope, architecture, acceptance criteria, and final synthesis.
- Researcher: investigates external libraries, Google/fast-flights behavior, constraints, and evidence.
- Developer: implements one narrowly scoped task and adds tests.
- Reviewer: audits the proposed diff for correctness, regressions, security, data integrity, and maintainability.
- ML specialist: only later, after a clean historical dataset and baseline evaluation exist.

AI agents must propose changes through artifacts/commits/PRs. They must not silently redefine architecture.

## Definition of done
A task is not done because the code runs once. It is done when:
1. behavior is specified;
2. implementation is isolated;
3. deterministic tests exist;
4. relevant failure modes are considered;
5. review has been performed;
6. no secret/cost/architecture regression was introduced;
7. the change is ready for a pull request.
