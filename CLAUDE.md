# MadCP — CLAUDE.md

MadCP (madhu) is a multi-tier MCP task orchestration server. External orchestrators
submit tickets; internal workers (currently Gemma via Ollama) execute them and return
results synchronously.

## Tech stack

- Python 3.11+, `mcp` SDK, Pydantic v2, SQLite (stdlib), `httpx`, `rich`, `pyyaml`
- Tests: `pytest`, `pytest-asyncio`, `respx`

## Off-limits directories — do not write to these

- `data/`     — SQLite database (source of truth)
- `logs/`     — append-only JSONL run log
- `tickets/`  — auto-generated markdown sync (derived from SQLite)
- `scratch/`  — Coordinator-authored smoke scripts

## Key concepts

**Tickets** are the only primitive. All work flows through them. Agents never talk
directly to each other.

**Tiers** are named from the `KRISHNAS` list (see `MadCP.md`). Active tiers: Adi
Purusha (top, orchestrator entry point) and Hamsa (leaf, Gemma workers).

**Touch protocol** — a ticket is worked by exactly one agent at a time. Acquire →
work → release. Atomic via SQLite `BEGIN IMMEDIATE`.

**MTap** — leaf workers are ephemeral: spawned, loaded, executed, terminated. One
process per ticket.

**`param-aatma`** — the internal name for the external orchestrator (Claude Code /
Opus). Every inbound ticket's `created_by_agent` defaults to `param-aatma`.

**Failure semantics** — on failure, a ticket is killed and a new forwarded ticket is
created with all prior failure notes appended. After 3 forwards, the ticket is
`aborted`.

**SQLite is source of truth.** Markdown in `tickets/` is derived and may be stale by
one write cycle.

## Running the server

```bash
python server.py                    # MCP server (stdio)
python dashboard.py                 # TUI dashboard (separate terminal)
python dashboard.py --filter Hamsa  # filtered view
```

## Running tests

```bash
pytest                    # all tests
pytest tests/test_store.py -v
```

## Architecture

See `MadCP.md` for full architecture, locked decisions, and build history.
