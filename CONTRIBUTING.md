# Contributing to Argus Council

Thanks for helping. This guide covers how to set up the project, what a good change looks like,
and how to get it merged. By taking part you agree to follow the
[code of conduct](CODE_OF_CONDUCT.md).

## Reporting bugs and asking for features

- **Security problems**: do not open an issue. Follow [SECURITY.md](SECURITY.md).
- **Bugs**: open a [bug report](https://github.com/morpheuslord/Argus-Council/issues/new?template=bug_report.yml)
  with the version (commit), how you run it (Docker stack or local), the steps, and what you
  expected. Logs and the run's event log (`GET /api/v1/runs/{id}/events`) help most; remove any
  keys or personal data first.
- **Features**: open a [feature request](https://github.com/morpheuslord/Argus-Council/issues/new?template=feature_request.yml)
  that describes the problem before the solution.

## Setting up

You need Python 3.12+ with [uv](https://docs.astral.sh/uv/), Node.js 22 with npm, and Docker for
the full stack.

```bash
# backend: http://localhost:8000 (SQLite, no Redis needed)
cd backend
uv sync
uv run uvicorn argus.main:create_app --factory --reload

# frontend: http://localhost:5173, proxies /api to the backend
cd frontend
npm ci
npm run dev
```

Without provider keys, councils use the offline `mock` provider, which is enough for nearly all
development and every test. To run the production-style stack: `./scripts/deploy.sh --smoke`.

## Checks

Run what CI runs before you push:

```bash
cd backend
uv run ruff check . && uv run ruff format --check .
uv run pytest

cd ../frontend
npm run lint && npm run typecheck && npm test && npm run build
```

Changing the database schema needs an Alembic migration in `backend/alembic/versions/`; CI checks
that migrations apply, match the models and downgrade cleanly on Postgres.

If you touch the documentation (`docs/`, `mkdocs.yml`), build it with warnings as errors:

```bash
./scripts/gen-openapi.sh    # when routes or schemas changed
uv run --no-project --with-requirements tools/docs-requirements.txt mkdocs build --strict
```

## Making a change

- Keep a pull request to one change, with tests for new behaviour and for the bug it fixes.
- Every claim in the docs must be true of the code: update `docs/` and the README in the same pull
  request when behaviour, configuration or the API changes, and add a line to the `Unreleased`
  section of [CHANGELOG.md](CHANGELOG.md).
- Follow the existing style: `ruff` for Python (line length 110), ESLint and TypeScript strictness
  for the frontend, and plain, specific wording in user-facing text.
- Never commit secrets, `deploy/.env`, database files or run artifacts.
- Security-sensitive areas (auth, the sandbox, web fetching, protocol endpoints, exports, uploads)
  need a test that shows the guard holds.

## Commits and pull requests

- Write commit messages in the imperative mood with a short summary line and, when it helps, a body
  that says why.
- Fill in the pull request template, link the issue it closes, and make sure CI is green.
- A maintainer reviews every pull request. Expect questions; they are about the code, not about you.

## Licence

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
