# AGENTS.md

## Project Role
AI coding agents (Claude Code, Antigravity, etc.) are implementers, not
autonomous architects.

Follow approved architecture, technology decisions, repository structure,
and the current project phase. Do not move to another phase or make major
architectural changes without human approval.

## Current Phase


Phase 3 — Repository & Development Foundation.
Application development starts in Phase 4.

to:

Phase 4 — Backend Core / FastAPI.

Do not modify any other section or file.

Do not change architecture rules.

After editing, verify the diff and stop.
Do not begin Phase 4.3.
Do not commit.

## Simplicity
Prefer the smallest production-quality solution.

- Reuse existing code before creating abstractions.
- Do not create files, classes, interfaces, wrappers, or dependencies
  without a current requirement.
- Avoid speculative architecture and future-proofing.
- Prefer simple modules over unnecessary layers.
- Do not duplicate framework functionality.
- Keep the codebase small, readable, and maintainable.
- Add complexity only when a concrete requirement justifies it.

## Development Rules
- Inspect relevant existing code before modifying it.
- Make the smallest reasonable change.
- Keep code simple, readable, and maintainable.
- Avoid unnecessary abstractions and dependencies.
- Reuse existing utilities when appropriate.
- Do not create unnecessary files.
- Do not modify unrelated files.

## Dependencies
- Check whether the standard library or an existing dependency is sufficient.
- Add dependencies only when required.
- Prefer stable, maintained libraries.
- Avoid duplicate tools or libraries serving the same purpose.
- Do not add dependencies speculatively.

## AI / RAG
Do not introduce new LLMs, embedding models, vector stores, or agent
frameworks without approval.

Keep provider-specific code behind replaceable interfaces and avoid direct
provider SDK calls from application/business logic.

## Database
PostgreSQL + pgvector is the approved database foundation.

Do not create or modify application schemas without an approved task.
Do not bypass migrations once they are established.
Do not introduce another database without approval.

## Testing
For meaningful code changes:
- Run relevant tests.
- Run relevant lint/type checks when applicable.
- Fix failures caused by the change.
- Report what was changed and verified.
- Never claim a test passed unless it was actually run.
- Do not create unnecessary tests or tooling just to make a check pass.

## Security
- Never commit `.env` or secrets.
- Never hardcode passwords, API keys, or tokens.
- Do not print secrets in logs or responses.
- Do not disable security controls just to make something work.

## Git
- Do not commit unless explicitly requested.
- Do not force push, reset, or discard user work without approval.
- `main` is the stable branch.
- Use `feature/<short-description>`, `fix/<short-description>`, or
  `chore/<short-description>` for development branches.

## Workflow
1. Understand the task.
2. Inspect relevant files.
3. Make the required changes.
4. Verify the changes.
5. Report what changed and what was verified.
6. Stop when the requested task is complete.

Ask for approval before:
- changing approved architecture or technology
- adding a new external service
- changing production infrastructure
- performing destructive database operations
- overwriting existing user work