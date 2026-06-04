# Contributing

Thank you for considering a contribution. This document describes the
coding standards and review workflow used in this repository.

## Coding standards

- **Python 3.11 required.** Use modern syntax (PEP 604 `X | Y` unions).
- **Type hints on every public function** in the matching, sources,
  and storage layers. The legacy bulk detector (`matching/legacy.py`)
  is exempt because it predates the split.
- **Docstrings in English, Google style.** Arabic appears only inside
  `Examples:` blocks when the example value is naturally Arabic.
- **Comments explain the why, not the what.** Default to no comment.
  When a comment is warranted, prefix it with the reason.
- **No emojis** in code, comments, docstrings, READMEs, or commit
  messages. The dashboard HTML templates are the only exception.

## Tooling

- **ruff** for linting and formatting.
- **mypy** for the matching and core layers.
- **pytest** for tests.
- **pre-commit** to run the above on every commit locally.

Install once:

```powershell
pip install -e ".[dev]"
pre-commit install
```

## Branching and pull requests

- Work on a topic branch off `main`. Name it `<area>/<short>`, for
  example `matching/template-only-tuning`.
- Keep each pull request focused. Mechanical refactors should be a
  separate PR from behavior changes.
- CI (ruff, mypy, pytest) must pass before review.

## Commit messages

- Imperative mood ("Add early-stop guard to fetch_latest").
- Subject under 72 characters.
- Body explains the why.

## Releasing

- Bump the version in `pyproject.toml` following SemVer.
- Add a `CHANGELOG.md` entry.
- Tag the merge commit with `vX.Y.Z`.
