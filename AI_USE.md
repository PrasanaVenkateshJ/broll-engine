# AI_USE.md — AI tools used in this project

| Tool | Used for |
|---|---|
| Claude Code (VS Code extension) | Pair-programming: scaffolding, writing code and tests from my design rules in CLAUDE.md, explaining code, debugging. I reviewed, ran and can explain every change. |
| Claude (claude.ai) | Analysing the brief and planning the architecture and build phases. |

## Log (one line per step)
- Phases 1-2: Claude Code built the skeleton (config, JSON logs, errors, health/ready, Dockerfile, compose, CI), the job API (JSON + upload), idempotency, signed download links, auth + rate limit, worker with atomic claim, signed callbacks and 48 tests (47 pass locally, 1 MinIO test runs only in Docker/CI).
