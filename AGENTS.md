# Repository Guidelines

## Project Structure & Module Organization
- `strictmode/` houses the runtime package; keep CLI entry points in `cli.py`, configuration in `config.py`, and domain logic under `engine/`, `datasrc/`, and `rules/`.
- `tests/` mirrors the package layout (`test_cli.py`, `test_datasource.py`, etc.)—add new tests beside the feature you touch.
- `pyproject.toml` declares dependencies and the `strictmode` console script; update this file when you introduce new runtime requirements.

## Build, Test, and Development Commands
- `python -m venv .venv && source .venv/bin/activate` creates an isolated Python 3.11 environment.
- `pip install -e .[development]` installs the package with development extras (e.g., `pytest`).
- `strictmode --help` verifies the Typer CLI wiring; run subcommands like `strictmode buy TSLA 10 --dry-run` against demo data sources.
- `pytest` executes the full regression suite; use `pytest tests/test_cli.py -k chandelier` to target a subset while iterating.

## Coding Style & Naming Conventions
- Follow idiomatic Python: 4-space indentation, type hints, dataclasses for configuration, and descriptive function names (`trailing_stop`, `load_settings`).
- Group imports by standard library, third-party, then local modules; keep Typer command functions pure and move side effects into helpers, as done in `DependencyContainer`.
- Maintain module names in snake_case and favor explicit exports via `__all__` when exposing new APIs.

## Testing Guidelines
- Use `pytest` with function names that describe behavior (`test_chandelier_handles_gap`).
- When adding CLI functionality, add integration-style tests in `tests/test_cli.py` that exercise Typer callbacks and validate journal updates.
- Mock external services (`AlphaVantageDataSource`, `TelegramNotifier`, `IBBroker`) so tests stay deterministic.

## Commit & Pull Request Guidelines
- Write imperative, scope-focused commit messages (e.g., "Implement StrictMode backend" from `349414d`).
- Keep PRs small, include a short description of intent, reference related issues, and attach screenshots or CLI transcripts when behavior changes.
- Highlight configuration or migration steps in the PR body so deployers can update `.env` values or database files before rollout.

## Configuration & Secrets
- All runtime settings load from environment variables prefixed with `STRICTMODE_` (see `strictmode/config.py`).
- Never commit real API keys or Telegram tokens; document required variables (`STRICTMODE_DATA_API_KEY`, `STRICTMODE_TELEGRAM_BOT_TOKEN`) in PRs when introducing new dependencies.
