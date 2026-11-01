# Contributing

Jarvis controls a real computer, so changes should preserve its approval and privacy boundaries.

## Development setup

```bash
uv sync --extra dev
source .venv/bin/activate
```

Without `uv`, create a virtual environment and run `python -m pip install -e '.[dev]'`.

Run the complete verification suite before opening a pull request:

```bash
ruff check jarvis tests
ruff format --check jarvis tests
swift format lint --recursive macos
pytest -q
python -m compileall -q jarvis
./scripts/build_macos.sh
```

Use `ruff format jarvis tests` and `swift format format --in-place --recursive macos` to apply the formatters.

## Safety expectations

- Keep reads within configured roots.
- Require a fresh human approval for side effects.
- Treat files, websites, tool output, memories, and skills as untrusted context.
- Never commit API keys, databases, traces, exports, user-authored skills, or model artifacts.
- Add or update tests when changing permissions, recovery, approvals, tracing, or dataset selection.

## Generated and private files

The `data/`, `exports/`, `models/`, `dist/`, and `.build/` directories are local runtime or build state and are intentionally ignored by Git.

## Integration checks

On a configured Apple silicon Mac, run the isolation and local training checks:

```bash
uv sync --extra dev --extra training
JARVIS_TEST_SHURU=1 JARVIS_TEST_NETWORK=1 JARVIS_TRAINING_TEST=1 pytest -q
```

Shuru and Squid must be installed using Setup Jarvis or
`zsh scripts/setup_sandbox.sh`. The training test uses synthetic data and a tiny
local model. The billable OpenAI integration test remains separately opt-in via
`JARVIS_LIVE_TEST=1`; never use personal files as its fixtures.
