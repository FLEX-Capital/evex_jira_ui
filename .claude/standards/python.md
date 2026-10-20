# Python Coding Standards

## Tooling

| Tool     | Purpose                       | Command                                                        |
| -------- | ----------------------------- | -------------------------------------------------------------- |
| `uv`     | Package management            | `uv add <pkg>`, `uv sync`                                      |
| `pytest` | Testing                       | `uv run pytest`                                                |
| `ruff`   | Linting and formatting        | `uv run --frozen --no-sync python tools/check_python.py --all` |
| `mypy`   | Type checking (required)      | `uv run --frozen --no-sync mypy`                               |
| `trunk`  | Orchestrates all of the above | `trunk fmt && trunk check`                                     |

Never use `pip install` directly — always `uv add`.

## Style

- Line length: **100 characters**
- Ruff alone owns Python formatting and import order. Run `uv run --frozen --no-sync ruff format .` to format, then `uv run --frozen --no-sync python tools/check_python.py --all` to check. Commit and push hooks run Ruff and one complete native mypy batch against isolated index bytes/config; Trunk retains non-Python gates. Historical archive source is excluded from lint but every tracked notebook obeys the output guard.

## Type hints

All public function and method signatures must have type hints.

```python
# correct
def calculate_premium(base: float, risk_factor: float) -> float: ...


# wrong — no type hints
def calculate_premium(base, risk_factor): ...
```

Types belong in signatures only — never repeat them in docstrings.

## Docstrings

Required on all public functions and classes. Use **Google style**. Types go in the signature, not the docstring.

```python
def calculate_premium(base: float, risk_factor: float) -> float:
    """Calculate the risk-adjusted premium.

    Args:
        base: Base premium before adjustment.
        risk_factor: Multiplier between 1.0 and 5.0.

    Returns:
        Adjusted premium amount.

    Raises:
        ValueError: If risk_factor is outside the allowed range.
    """
```

Not required on: `__init__`, magic methods, private functions, modules, packages.

## Naming conventions

| Kind                    | Convention                    | Example                               |
| ----------------------- | ----------------------------- | ------------------------------------- |
| Module / package        | `snake_case`                  | `claims_processor.py`                 |
| Function / method       | `snake_case`                  | `def calculate_premium()`             |
| Variable / argument     | `snake_case`                  | `policy_id`                           |
| Class                   | `PascalCase`                  | `class ClaimsProcessor`               |
| Exception               | `PascalCase` + `Error` suffix | `class ValidationError`               |
| Constant (module-level) | `UPPER_SNAKE_CASE`            | `MAX_RETRY_COUNT = 3`                 |
| Type alias              | `PascalCase`                  | `PolicyId = NewType("PolicyId", str)` |
| Private symbol          | leading `_`                   | `_parse_raw_event()`                  |
| Test file               | `test_<module>.py`            | `test_claims_processor.py`            |
| Test function           | `test_<behaviour>`            | `test_rejects_expired_policy()`       |

Ruff enforces the naming rules (N8xx) — CI fails on violations.

## Testing

- Framework: `pytest`
- All tests live under `tests/`
- Maintain **80%+** application coverage; CI enforces the threshold and comments on scoped PRs.
- Test function names describe behaviour, not the method under test: `test_rejects_expired_policy`, not `test_calculate_premium`
- No untested public functions

## Environment variables

- Never read `.env` files in application code — no `dotenv`, no manual parsing
- Read from `os.environ` only; env injection is a deployment concern
- Inject locally via `uv run --env-file .env`
- Document required vars in the project README

## Dependencies

- Add with `uv add <package>`
- Commit `uv.lock` — it pins the full dependency graph for reproducible CI
- Prefer stdlib over third-party where functionality is equivalent
- Ask before adding new dependencies

## Error handling

- Never silently swallow exceptions
- Raise specific exception types; avoid bare `except Exception`
- Handle errors at the appropriate boundary, not everywhere
