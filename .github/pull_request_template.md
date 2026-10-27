## What and why

<!-- What this changes and the problem it solves. Link the issue: "Closes #123". -->

## How it was tested

<!-- Tests added or changed, and anything you checked by hand (UI, API calls, the Docker stack). -->

## Checklist

- [ ] Backend: `uv run ruff check . && uv run ruff format --check . && uv run pytest`
- [ ] Frontend: `npm run lint && npm run typecheck && npm test && npm run build`
- [ ] Schema changes come with an Alembic migration
- [ ] Docs (`docs/`, README) and the current version's section of `CHANGELOG.md` match the change
- [ ] `docs/api/openapi.json` regenerated with `./scripts/gen-openapi.sh` if routes or schemas changed
- [ ] No secrets, `.env` files, databases or run artifacts committed
