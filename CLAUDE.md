# EVEX Jira UI

Streamlit analytics dashboard for EVEX Jira service desks

## Toolchain

- Python 3.12
- Source: root-level Python modules; Streamlit entry point: `app.py`
- Tests: `tests/`
- This existing application retains its flat module layout and runtime dependencies.

## Standards

@.claude/standards/python.md
@.claude/standards/continuous_delivery.md

## Hooks

Two quality gates active in this project:

| Layer       | Trigger         | Action                                        |
| ----------- | --------------- | --------------------------------------------- |
| git (trunk) | `git commit`    | `trunk fmt` and `trunk check` on staged files |
| git (trunk) | `git push`      | `trunk check` — blocks on lint failures       |
| Claude Code | file edit/write | `trunk fmt` on the changed file               |
