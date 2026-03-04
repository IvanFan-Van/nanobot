# AGENTS.md — nanobot Repository Guide

## Project Overview

`nanobot` is a lightweight personal AI assistant framework (Python package `nanobot-ai`).
It connects to multiple messaging platforms (Telegram, Discord, Slack, WhatsApp, etc.) and
drives an LLM agent loop with tools, skills, memory, and cron scheduling.
A small TypeScript/Node.js bridge (`bridge/`) handles the WhatsApp channel via Baileys.

---

## Build, Run & Test Commands

### Python (primary package)

```bash
# Install all dependencies (use uv)
uv sync

# Install with optional Matrix support
uv sync --extra matrix

# Install with dev dependencies
uv sync --extra dev

# Run the CLI
uv run nanobot --help

# Run all tests
uv run pytest

# Run a single test file
uv run pytest tests/test_loop_save_turn.py

# Run a single test function
uv run pytest tests/test_loop_save_turn.py::test_save_turn_strips_runtime_context

# Run tests with verbose output
uv run pytest -v

# Lint (ruff — check only)
uv run ruff check .

# Lint with auto-fix
uv run ruff check --fix .

# Format check
uv run ruff format --check .

# Format apply
uv run ruff format .
```

### TypeScript bridge (`bridge/`)

```bash
# Install dependencies
npm install

# Build TypeScript → dist/
npm run build

# Build and run (dev mode)
npm run dev

# Start pre-built binary
npm start
```

### Docker

```bash
docker-compose up --build
```

---

## Repository Layout

```
nanobot/           Main Python package
  agent/           AgentLoop, ContextBuilder, MemoryStore, SkillsLoader, SubagentManager
  agent/tools/     Tool ABC, registry, and all built-in tools
  bus/             InboundMessage / OutboundMessage events + MessageBus queue
  channels/        One file per platform + BaseChannel ABC + ChannelManager
  cli/             Typer CLI commands (onboard, gateway, agent, channels, status, provider)
  config/          Config file I/O + Pydantic schema models
  cron/            CronService + CronJob / CronSchedule types
  heartbeat/       HeartbeatService
  providers/       LLMProvider ABC + LiteLLM, OpenAI Codex, custom provider implementations
  session/         Session + SessionManager
  skills/          Bundled skill directories (each with a SKILL.md)
  templates/       Default workspace files copied on `nanobot onboard`
  utils/           Shared helpers
bridge/            WhatsApp bridge (TypeScript/Node.js)
tests/             Pytest test suite (16 files)
```

---

## Python Code Style

### Formatting & Linting

- **Ruff** is the sole linter and formatter; no Black, no isort separately.
- `line-length = 100`, `target-version = "py311"`.
- Enabled rule sets: `E` (pycodestyle errors), `F` (pyflakes), `I` (isort), `N` (pep8-naming), `W` (pycodestyle warnings).
- `E501` (line-too-long) is **ignored** — lines slightly over 100 chars are acceptable.
- Run `ruff check --fix . && ruff format .` before committing.

### Imports

```python
# 1. Standard library
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

# 2. Third-party
from loguru import logger
from pydantic import BaseModel

# 3. Local
from nanobot.config.schema import Config
from nanobot.bus.events import InboundMessage

# Avoid circular imports: use TYPE_CHECKING guard or lazy imports inside functions
if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop
```

- Always keep imports isort-sorted (ruff handles this automatically).
- Lazy imports inside functions/methods are acceptable when needed to break circular deps.

### Naming Conventions

| Kind | Convention | Example |
|---|---|---|
| Classes | `PascalCase` | `AgentLoop`, `ToolCallRequest` |
| Functions / methods | `snake_case` | `run_agent_loop`, `validate_params` |
| Private methods | `_snake_case` | `_parse_response`, `_setup_env` |
| Constants / module-level frozen sets | `UPPER_SNAKE` | `EXIT_COMMANDS`, `PROVIDERS` |
| Type aliases | `_PascalCase` or `UPPER_SNAKE` | `_ALLOWED_MSG_KEYS` |
| Pydantic models | `PascalCase` | `ProviderConfig`, `ChannelConfig` |

### Type Annotations

- Every function/method must have full parameter and return-type annotations.
- Use Python 3.10+ union syntax: `str | None`, `list[str] | None` (not `Optional[str]`).
- Use built-in generic types: `dict[str, Any]`, `list[str]` (not `Dict`, `List` from `typing`).
- Use `@dataclass` / `@dataclass(frozen=True)` for value objects.
- Use Pydantic `BaseModel` / `BaseSettings` for config and validated data contracts.
- `Any` is acceptable for LLM message dicts and JSON-like payloads.

### Error Handling & Logging

- **All logging via `loguru`**: `logger.info(...)`, `logger.warning(...)`, `logger.error(...)`, `logger.exception(...)`.
- At execution boundaries (tool runs, LLM calls, channel handlers) catch `Exception` broadly,
  log it, and return a user-visible error string — do **not** re-raise.
- **Always re-raise `asyncio.CancelledError`** after logging; never swallow it.
- Use `try/finally` for cleanup (MCP server stacks, cron locks, terminal state restoration).

```python
try:
    result = await self._call_tool(name, params)
except asyncio.CancelledError:
    logger.warning("Tool call cancelled: {}", name)
    raise
except Exception as e:
    logger.exception("Tool {} failed", name)
    return f"Error: {e}"
finally:
    self._cleanup()
```

### Async Patterns

- All I/O is `async/await`; blocking calls must be avoided in the event loop.
- Use `asyncio.create_task()` for fire-and-forget background work; keep a reference to avoid GC.
- Use `asyncio.wait_for(..., timeout=N)` for interruptible polling loops.
- Use `weakref.WeakValueDictionary` for caches/locks keyed by session to avoid memory leaks.

### Docstrings

- Every module file gets a one-liner module-level docstring.
- Every class gets a docstring describing its responsibility.
- Public and abstract methods get docstrings; private helpers may omit them.
- Use Google-style sections (`Args:`, `Returns:`, `Raises:`) on public APIs only.

---

## TypeScript (bridge/) Code Style

- **TypeScript strict mode** is on; all `strict` compiler checks apply.
- Target: `ES2022`, module system: `ESNext`.
- Use `import`/`export` (ESM only — `"type": "module"` in package.json).
- No ESLint or Prettier config is present; keep style consistent with existing files.
- Logger: `pino` (not `console.log`).
- Node.js ≥ 20 required.

---

## Testing Guidelines

- Framework: `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`).
- All tests are **top-level functions** — no test classes.
- Use `pytest.fixture` for setup; prefer `tmp_path` for temporary directories.
- Construct objects under test with `ClassName.__new__(ClassName)` to bypass `__init__`
  when full initialization is not needed.
- Use `unittest.mock.patch` / `monkeypatch` for mocking external dependencies.
- Use `typer.testing.CliRunner` for CLI command tests.
- Async test functions are automatically detected — no `@pytest.mark.asyncio` needed
  (it is still acceptable to add it for clarity).
- Tests live in `tests/`; name files `test_<module>.py` and functions `test_<scenario>`.

---

## Key Design Patterns

- **Tool ABC** (`nanobot/agent/tools/base.py`): all tools subclass `Tool`, declare a JSON schema,
  and implement `async execute(params) -> str`. Tool results are always strings.
- **Channel ABC** (`nanobot/channels/base.py`): channels subclass `BaseChannel`, receive
  `InboundMessage` from the bus, and emit `OutboundMessage` back.
- **Config** is validated by Pydantic at load time from `config.json`; env vars with prefix
  `NANOBOT_` can override fields (via `pydantic-settings`).
- **LLM providers** subclass `LLMProvider` and are registered in `providers/registry.py`.
  Use `LiteLLMProvider` as the default — it supports all major models via LiteLLM routing.
- **Skills** are directories under `nanobot/skills/` or the user workspace, each containing
  a `SKILL.md` that is injected into the agent system prompt.

---

## Python Version & Package Manager

- Requires **Python ≥ 3.11**.
- Use **`uv`** (not pip) for all dependency management. The lockfile is `uv.lock`.
- Build backend: **hatchling** (`pyproject.toml`).
