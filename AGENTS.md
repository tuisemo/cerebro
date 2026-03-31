# AGENTS.md

## Purpose

This repository contains `Cerebro`, a Windows-first Discord bot that delegates coding and Q&A work to the Factory `droid` CLI. The bot turns Discord threads into resumable Droid sessions with per-thread workspaces, task persistence, queueing, and streamed tool/assistant output.

## Stack

- Python 3.12+
- `discord.py`
- `python-dotenv`
- SQLite (`droid_tasks.db`)
- External runtime dependencies: `droid`, `git`

## Entry Points

- Package script: `cerebro = "cerebro.app:main"` in `pyproject.toml`
- Module entry: `python -m cerebro`
- Direct run: `python cerebro/app.py`

## Local Setup

1. Install dependencies:
   - `uv sync`
   - or `pip install -e .`
2. Copy `.env.example` to `.env`
3. Ensure these executables are available on PATH:
   - `droid`
   - `git`
4. Start the bot with one of:
   - `uv run cerebro`
   - `python -m cerebro`

## Important Environment Variables

- `DISCORD_BOT_TOKEN`: required
- `FACTORY_API_KEY`: optional; if set, `app.py` exports it into the process environment
- `DEFAULT_MODEL`: default `custom:MiniMax-M2.7`
- `WORKSPACES_DIR`: default `./droid_workspaces`
- `MAX_CONCURRENT_TASKS`: default `2`

Also read by code:

- `TASK_TIMEOUT_MINUTES` (currently not enforced by runtime flow)
- `LOG_DIR`
- `LOG_LEVEL`
- `LOG_MAX_BYTES`
- `LOG_BACKUP_COUNT`

## Repository Map

- `cerebro/app.py`: main orchestration layer; bot startup, slash commands, queue workers, message interception, task execution lifecycle
- `cerebro/runner.py`: launches `droid exec`, reads line-delimited JSON events, captures `session_id`
- `cerebro/handler.py`: converts Droid events into Discord messages, approvals, dashboard updates, and streamed assistant output
- `cerebro/workspace.py`: thread-scoped workspaces, repo clone/use-directory/temp workspace logic, cleanup, patch generation
- `cerebro/registry.py`: SQLite persistence for task metadata, status, parsed task data, and resumable `session_id`
- `cerebro/parser.py`: parses `/task` prompt, extracts `repo:` / `workspace:`, infers file-operation mode from keywords
- `cerebro/ui.py`: single-message dashboard embed
- `cerebro/throttle.py`: per-thread Discord rate limiter
- `cerebro/__main__.py`: `python -m cerebro` entry

## Runtime Flow

1. `app.py` loads env/config, creates the Discord bot, and initializes:
   - `WorkspaceManager`
   - `TaskRegistry`
   - background cleanup loop
   - `MAX_CONCURRENT_TASKS` worker coroutines
2. `/task` parses the prompt and enqueues a job. If invoked outside a thread, the bot creates a public thread first.
3. Worker coroutine calls `_execute()`:
   - sends a `TaskDashboard`
   - restores prior `session_id` if the thread is resumable
   - registers task state in SQLite before and after workspace resolution
   - resolves workspace from `repo:`, `workspace:`, or temp mode
4. `DroidTask.run()` executes:
   - `droid exec --output-format debug --auto medium --cwd <cwd> -m <model> [--session-id ...] <prompt>`
5. `DroidEventHandler` streams assistant text and tool activity back to Discord.
6. On completion, the task status becomes `waiting`; if the task touched files/repo mode, a patch may be generated and uploaded.
7. Further user messages in the same thread can continue the prior conversation by reusing the persisted `session_id`.

## Task and Session Model

- One SQLite row per Discord thread (`thread_id` is the primary key).
- Primary statuses in real use:
  - `active`: run in progress
  - `waiting`: run finished, thread can continue
  - `completed`: set by manual end command; still resumable in current code
- `closed` exists as a constant but is not currently used by runtime flow.
- On startup, stale DB rows marked `active` are reset to `waiting`.
- `/new` clears the workspace and `session_id`, but keeps the thread record so a fresh conversation can begin in the same thread.

## Workspace Rules

- `repo:` mode expects a local filesystem path and uses `git clone --local`.
- If a `repo:` path does not exist, code creates the directory and initializes git instead of failing immediately.
- `workspace:` mode uses the directory directly; it is restricted to paths under `WORKSPACES_DIR.parent`.
- Temp/QA mode still creates a git-backed temp directory under `WORKSPACES_DIR/<thread_id>`.
- `cleanup_workspace()` deletes the managed workspace directory with `shutil.rmtree()`.
- Auto cleanup removes non-active workspaces older than 24 hours and deletes their DB records.

## Risk Controls

- `handler.py` inspects `Execute` / `execute_command` tool calls.
- High-risk command patterns such as `rm -rf`, `format`, `shutdown` require Discord button approval.
- Moderate-risk execute calls are announced but continue automatically.
- `MessageThrottle` enforces a minimum interval between sends/edits to reduce Discord 429 risk.

Important caveat: risk approval is implemented at the event-handling layer. Keep this behavior intact and be careful when changing tool-call flow, process termination, or event semantics.

## Windows-Specific Behavior

- This codebase is intentionally Windows-oriented.
- `runner.py` appends a Windows system note to every Droid prompt, telling Droid to prefer PowerShell/CMD syntax and Windows paths.
- Do not introduce Linux-only command assumptions into runtime code or examples.
- If troubleshooting locally, prefer Windows-native checks such as `where droid` and `where git`.

## Patch Generation Caveats

- After repo/file tasks, `workspace.py` may run:
  - `git add .`
  - `git status --porcelain`
  - `git commit -m "Auto-commit by Droid"`
  - `git diff HEAD~1`
- This mutates the sandbox repo state.
- Patch generation can be fragile in fresh repos or when git identity is not configured.

## Commands and Behaviors Not to Break

- Slash commands:
  - `/task`
  - `/status`
  - `/cleanup`
  - `/new`
- Thread-based interaction model
- Resume flow via persisted `session_id`
- Per-thread workspace isolation
- Background queue + concurrency semaphore model
- Streaming assistant/tool output to Discord
- High-risk approval UX

## Validation Guidance for Agents

There is no formal test suite configured in this repository today. Before concluding code changes, at minimum run the checks that are actually available in the environment and relevant to your change.

Common repo-level checks:

- `python -m compileall cerebro`
- Start-path sanity check when changing startup/runtime behavior:
  - `python -m cerebro`

If the change affects packaging or dependency wiring, also review:

- `pyproject.toml`
- `.env.example`
- `README.md`

## Known Implementation/Documentation Gaps

When reading README claims, trust source code first:

- README describes a simpler lifecycle; actual code relies heavily on `waiting` as the resumable steady state.
- README frames non-file tasks as pure Q&A, but code still creates a temp git workspace.
- README does not call out all logging/env knobs or the workspace path restriction.

## Agent Working Style for This Repo

- Read `pyproject.toml`, `.env.example`, and the affected `cerebro/*.py` modules before editing.
- Keep dependencies minimal; follow existing standard-library-heavy style.
- Preserve current module boundaries unless a refactor is explicitly requested.
- Be especially careful in:
  - `app.py` task lifecycle
  - `runner.py` process/event handling
  - `workspace.py` destructive filesystem operations
  - `registry.py` persistence semantics
- If you change event schemas or task statuses, trace every caller across `app.py`, `runner.py`, `handler.py`, and `registry.py`.
