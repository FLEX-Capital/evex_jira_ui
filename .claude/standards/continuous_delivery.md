# Delivery standards

## Branches

- `main` — always deployable; never commit directly
- `feature/<slug>` — one branch per slice of work, short-lived (hours to a couple of days)
- `fix/<slug>` — bug fixes
- Branch names come from the Linear issue where possible

## Commits

Conventional format: `<type>: <subject>` — subject under 72 chars.

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `ci`, `chore`

Each commit must be safe to deploy on its own.

## Keeping branches current

Rebase onto `main`, never merge it in:

```bash
git fetch origin && git rebase origin/main
```

## Pull requests

- Small and reviewable in one sitting
- CI must be green before merge
- Squash or rebase merge onto `main` — no merge commits
- Delete branch after merge
