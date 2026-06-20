# MadCP

MadCP (Madhu Context Protocol) is a multi-tier MCP server for task orchestration. An external orchestrator submits a ticket over MCP; MadCP routes it through a configurable hierarchy of tiers down to an ephemeral local-model worker, which executes the task, validates its output, and returns the result synchronously. All state is held in SQLite. Markdown ticket files and a JSONL run log are derived views.

The internal engine is named `madhu` (Mediated Agent Delegation & Handoff Utility); it is the Python package name.

The v0 reference setup is Claude Code (Opus) as the orchestrator, generating Python functions executed by Gemma 12B via Ollama. The payload type, the worker model, and the tier depth are all pluggable — function generation is just the first payload type.

The design goal is a server you stand up once and point anything at. You host your own models, run MadCP on a machine somewhere, connect the agent of your choice — Claude Code today, eventually a browser extension to Claude on the web, or any MCP-speaking client — and submit work to it from anywhere. The hierarchy is yours to shape: add or remove tiers, scale the worker count per tier up or down, and swap the model behind any agent at any level at any time without touching code. The ticket is what makes this work — because every unit of work is a self-contained, persisted record rather than a live connection, an agent can pick up a ticket regardless of where it runs or what model backs it, solve it, and hand the result back through the same channel. v0 implements the spine of this; the rest is on the roadmap below.

## Architecture

```
  External orchestrator (Claude Code / Opus)
            │  internally: param-aatma
            │
            │  MCP over stdio  (synchronous — submit_ticket blocks
            │                    until the ticket reaches a terminal state)
            ▼
  ┌─────────────────────────────────────────────────────────┐
  │  MadCP  (madhu)                                           │
  │                                                           │
  │   MCP surface ── submit_ticket · list_tickets             │
  │                  check_ticket · health_check              │
  │        │                                                  │
  │        ▼                                                  │
  │   ┌──────────────┐     reads/writes      ┌─────────────┐  │
  │   │  Scheduler   │◄────────────────────► │   SQLite    │  │
  │   │ polls queue  │   (source of truth)   │  (truth)    │  │
  │   │ spawns leaf  │                       └──────┬──────┘  │
  │   │ workers      │                              │ derives │
  │   └──────┬───────┘                              ▼         │
  │          │ multiprocessing.Process       tickets/*.md     │
  │          │ (one per ticket, MTap)        logs/runs.jsonl  │
  │          ▼                                                │
  │   ┌──────────────────────────────┐                       │
  │   │  Tier: Adi Purusha  (T1)     │  routes, never executes│
  │   │  Tier: Hamsa        (leaf)   │  RISHIS pool · Ollama   │
  │   └──────────────┬───────────────┘                       │
  │                  │ Provider abstraction                   │
  │                  ▼                                        │
  │         local model (Gemma 12B via Ollama, v0)            │
  └─────────────────────────────────────────────────────────┘
                     │
                     ▼
          rich-based terminal dashboard
          (separate process, read-only on SQLite)
```

A ticket enters at `Adi Purusha` (tier 1), which routes but does not execute. The scheduler polls the queue, finds the ticket, and spawns a fresh `Hamsa` leaf worker as its own process. The worker acquires an exclusive *touch* on the ticket — atomic via SQLite `BEGIN IMMEDIATE` — calls the model through the provider layer, validates the output, writes the result, releases the touch, and exits. The result returns to the caller through the still-open synchronous MCP call.

Agents never communicate directly. All coordination is mediated through ticket state in the database.

## Requirements

- Python 3.11+
- A running [Ollama](https://ollama.com) instance (for the v0 provider)

## Install

```bash
cd ~/projects/madhu

# Preferred
uv sync

# Fallback
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Runtime deps: `mcp`, `pydantic>=2`, `httpx`, `pyyaml`, `rich`.
Dev deps: `pytest`, `pytest-asyncio`, `respx`.

## Run

Pull the model:

```bash
ollama pull hf.co/yuxinlu1/gemma-4-12B-coder-fable5-composer2.5-v1-GGUF:Q8_0
```

Ollama must be reachable at `http://localhost:11434` before the server starts.

Start the server:

```bash
python server.py
```

Logging goes to stderr. Stdout is reserved for the MCP protocol; writing to stdout corrupts the stream.

Start the dashboard in a separate terminal:

```bash
python dashboard.py
python dashboard.py --filter Hamsa     # scope to one tier
```

Dashboard keys: `q` quit, `r` force refresh, `f` toggle filter, `t` toggle log tail.

Register with Claude Code:

```bash
claude mcp add madcp -- python ~/projects/madhu/server.py
```

Run the tests:

```bash
pytest                       # everything
pytest tests/test_store.py   # one file
pytest -v --tb=short         # verbose, short tracebacks
```

## MCP surface

Four tools. Descriptions in the server are extensive and include inline example payloads, since the calling model reads them to decide how to use the surface.

| Tool | Behaviour |
|------|-----------|
| `submit_ticket(envelope, payload)` | Submit work. Blocks until the ticket reaches a terminal state (`done`, `failed`, `killed`, `aborted`) and returns the full ticket including its result. |
| `list_tickets(filter)` | List tickets, optionally filtered by status, tier, or assignee. |
| `check_ticket(id)` | Fetch the full current state of one ticket. |
| `health_check()` | Server status, scheduler liveness, queue depth, in-progress count, active tiers, last terminal timestamp. |

## Configuration

Each active tier owns a YAML file in `madhu/tiers/configs/`. The leaf config holds most operational tuning:

```yaml
tier_name: "Hamsa"
tier_level: 2
pool: "RISHIS"
mtap: true
max_parallel: 2
worker_timeout_seconds: 180
allowed_payload_types: ["function_spec"]
provider: "ollama"
provider_config:
  model: "hf.co/yuxinlu1/gemma-4-12B-coder-fable5-composer2.5-v1-GGUF:Q8_0"
  endpoint: "http://localhost:11434"
  temperature: 0.2
  timeout: 120
failure_policy:
  max_forwards: 3
  on_max: "abort"
```

Per-tier config controls the system prompt, worker pool, concurrency cap, failure policy, and model provider. Activating a tier is adding a YAML file; depth is arbitrary, and v0 populates two tiers.

## Swapping the model provider

Workers receive a prompt string and return raw model output. They never see tickets or schemas, and all response cleaning (channel-marker and code-fence stripping, AST validation that the output is exactly one function) happens in the worker, not the provider. This keeps providers small and swapping them local to one file plus one config line.

To move from Ollama to vLLM (or LM Studio, OpenRouter, or any OpenAI-compatible endpoint):

1. Add `madhu/workers/providers/vllm.py` implementing the `Provider` protocol — one method, `generate(prompt, model, temperature, timeout) -> str`.
2. Register it: add `"vllm": VLLMProvider` to `PROVIDER_REGISTRY` in `madhu/workers/providers/__init__.py`.
3. Point the tier at it via `provider` and `provider_config` in `hamsa.yaml`.

No worker code changes. A standard provider is under fifty lines. Later releases will move provider selection further toward configuration, so adding one needs no code at all.

## Design decisions

These are the choices that shape the system. Most differ from how comparable orchestrators tend to work; the rationale matters more than the novelty.

**SQLite is the single source of truth.** Markdown files and the JSONL log are derived; SQLite wins on any conflict. The consequence is that every action — transient agent state, failed attempts, the full forwarding chain — is queryable, append-only structured data for the life of the system. A failed ticket from months ago can still be inspected: which agent failed, the reason, the raw output, and what later agents did. This is also what makes later failure-pattern mining possible.

**The ticket is the only primitive, and it is origin-agnostic.** Agents do not hold live connections to each other or to the caller; they claim a ticket, work it, and write the result back. Because a ticket is a complete, persisted description of a unit of work, it does not matter where the agent that solves it runs, which model backs it, or when it picks the work up. This is the property that lets the hierarchy be reshaped, scaled, and re-provisioned at runtime: any agent that can read a ticket and write a result is a valid worker. Per-ticket outcomes are recorded against the agent that produced them, which leaves room to later score agents on what they actually solve.

**Failure forwards; it does not silently retry.** A failed ticket is killed. A new ticket is created carrying the full list of prior failure notes and is handed to a different worker, linked by `forwarded_from`. Failure notes accumulate across the chain rather than being overwritten. After `max_forwards` is exceeded the ticket is set to `aborted` — distinct from `killed`, which is external termination (operator or timeout).

**Leaf workers are ephemeral by default (MTap).** A leaf worker spawns as a fresh process, loads the model, processes one ticket, and dies — no warm pools, no surviving context, no state leakage between tickets. MTap is from the Telugu *Manishi Anna vadiki, maranam Tappadu*. It is configurable per tier; the leaf defaults to on, routing tiers default to off.

**The touch protocol is atomic.** A ticket is worked by exactly one agent at a time: acquire, work, release. Acquisition uses SQLite `BEGIN IMMEDIATE`, so concurrent acquire attempts produce exactly one winner.

**The MCP surface is synchronous.** `submit_ticket` blocks until a terminal state. The caller never polls or manages state. This bounds task duration to the caller's timeout, which is a deliberate constraint at v0 (see roadmap).

**Schema is versioned from day one.** Every ticket records a `schema_version`; migrations are append-only files applied on read. The universal envelope shape is identical at every tier; only the payload varies, discriminated by a `type` field.

**Naming is an ontology, not labels.** Tiers are drawn from `KRISHNAS`, twenty-four avatar names in canonical order from `Adi Purusha` (highest) to `Hamsa` (leaf). The external orchestrator inside MadCP is always `param-aatma`. Leaf workers draw from the `RISHIS` pool and display lowercase (`vasishtha`, `agastya`). Names encode relationships — `Hamsa` is both the leaf tier and, in another pool, the vahana of Saraswati, who separates essence from non-essence, which is the leaf worker's job.

## A run, end to end

Asking Opus (through Claude Code) to generate `parse_query_string(s: str) -> dict[str, str]`:

The caller submits a ticket and waits. MadCP queues it, spawns a leaf worker (e.g. `AdHa-vasishtha`), which calls the model, validates that the output is exactly one function, writes the result, and exits. The function returns to Opus synchronously.

The dashboard shows tier counts updating, the live worker with its lineage path and elapsed time, and the ticket landing in the recent list as `done`. In `tickets/`, a markdown file appears with the full envelope as YAML frontmatter, touch history with `[[wiki-links]]`, and the returned function; it opens in Obsidian. In `logs/runs.jsonl`, the event chain reads `mcp_submit_enter → worker_spawn → touch_acquire → ollama_call → ollama_result → touch_release → worker_exit → mcp_submit_exit`.

A spec with an impossible constraint exercises the failure path: the worker fails and forwards, three fresh workers each try and fail, the third forward exceeds `max_forwards=3`, and the scheduler sets the ticket to `aborted` with four accumulated failure notes.

## Known limitations

- Only the Ollama provider is implemented. The abstraction is in place but untested at the second-provider level.
- A worker name can recur across consecutive forwards in the same chain (random draw with an active-name collision check only).
- The dashboard's live elapsed time is approximate (anchored on ticket creation, not the current touch start).
- Concurrency has only been exercised at small scale (`max_parallel=2`, low ticket volume).

None are architectural holes; the system routes real tickets and returns correct results.

## License

Proprietary — All Rights Reserved. You are granted a license to run and use this software as provided. You may not modify, copy, redistribute, sublicense, sell, reverse-engineer, or create derivative works from it. This is not an open-source license; no rights beyond use are granted. See the `LICENSE` file for the full terms.

## Documentation

Deeper architectural detail lives in two companion documents: **MadCP** (public) for the canonical architecture, and a private counterpart holding the in-depth internals, build canon, and full decision history.