# MadCP — Architecture (v2.2)

> Public architecture document. Canonical for the system as built at v0.
> v2.2 supersedes v2.1; all amendments are integrated inline.
>
> **Public name:** MadCP (Madhu Context Protocol)
> **Package / engine:** `madhu` (Mediated Agent Delegation & Handoff Utility)
> **What it is:** a standalone, multi-tier, general-purpose task-orchestration MCP server.

---

## 1. Overview

MadCP is a Python server that speaks MCP over stdio. An external orchestrator submits a ticket; MadCP routes it through a configurable hierarchy of tiers down to an ephemeral local-model worker, which executes the task, validates its output, and returns the result synchronously. SQLite is the single source of truth. Markdown ticket files and a JSONL run log are derived views.

It is not a code-generation tool — code generation is the first payload type, not the purpose. It is not a framework written against; it is a daemon delegated to. It is not a peer-to-peer agent mesh — it is strictly hierarchical, and agents never communicate directly. All coordination is mediated through ticket state in the database.

The v0 reference configuration: Claude Code (Opus) as the external orchestrator, generating Python functions, executed by Gemma 12B via Ollama at the leaf tier.

---

## 2. Core principles (locked decisions)

These are non-negotiable for v0. Each survived the build cycle without amendment.

1. **Tickets are the only primitive.** All actions flow through tickets. Agents never talk directly.
2. **Universal envelope, tier-specific payload.** The envelope shape is identical at every tier; only the payload varies, discriminated by a `type` field.
3. **Schema versioning from day one.** Every ticket records `schema_version`. Migrations are append-only files applied on read.
4. **MTap by default for leaf workers.** Leaf workers are ephemeral: spawned, loaded, executed, terminated. No queue, no persistent state. Configurable per tier.
5. **Touch protocol.** A ticket is worked by exactly one agent at a time: acquire → work → release. Atomic acquire via SQLite `BEGIN IMMEDIATE`.
6. **Synchronous MCP surface.** The orchestrator submits a ticket; the MCP call blocks until the ticket reaches a terminal state.
7. **SQLite is source of truth. Markdown is derived.** SQLite wins on any conflict.
8. **Per-tier config.** Each active tier owns a YAML config (system prompt, allowed payloads, max parallel, failure policy, provider).
9. **Failure semantics — kill and forward.** A failed ticket is killed; a new ticket is created carrying all prior failure notes, linked via `forwarded_from`, and a different agent picks it up. No silent retries.
10. **N-tier from day one.** The architecture supports arbitrary depth from the `KRISHNAS` list. v0 populates two tiers.
11. **`failure_notes` is a list.** Each forwarded ticket carries an append-only list of failure entries from previous attempts; entries accumulate across the chain.
12. **Terminal dashboard from day one.** A `rich`-based TUI showing live agent and ticket state.

---

## 3. Ontology and naming

Naming is rooted in Hindu cosmology in Sanskrit and Telugu. It encodes architectural relationships rather than decorating them.

### KRISHNAS — the tier list

A code constant in `madhu/names.py` holding 24 names in canonical avatar-expansion order, from `"Adi Purusha"` (the primordial, highest) to `"Hamsa"` (the swan, the leaf). The list is the universe of possible tier names. The operator selects which subset is active at startup.

```
Adi Purusha, Sanaka, Varaha, Narada, Nara-Narayana, Kapila, Dattatreya,
Yajna, Rishabha, Prithu, Matsya, Kurma, Dhanvantari, Mohini, Narasimha,
Vamana, Parashurama, Vedavyasa, Rama, Balarama, Krishna, Buddha, Kalki, Hamsa
```

### The two active tiers (v0)

- **`Adi Purusha`** (Tier 1, top) — routes, does not execute. Receives incoming submissions. Config: `accepts_external: true`, `mtap: false`, `max_parallel: 1`, `allowed_payload_types: []`.
- **`Hamsa`** (Tier 2, leaf) — executes. Workers spawn as `multiprocessing.Process`, load the configured provider, call the model, validate output, write results, exit. Config: `mtap: true`, `max_parallel: 2`, `allowed_payload_types: ["function_spec"]`, `provider: "ollama"`.

### param-aatma

The internal name for the external orchestrator once it enters MadCP. Every inbound ticket is stamped `created_by_agent: "param-aatma"`. The external system's identity is opaque to MadCP — internally it is always `param-aatma`. (Roughly, "supreme self": the entity outside `madhu` that initiates all work.)

### MTap — *Manishi Anna vadiki, maranam Tappadu*

A Telugu phrase: roughly, "for a person — even the one who brings food — death is unavoidable." Operationally: leaf workers are ephemeral by default. A Hamsa worker spawns fresh, loads the model, processes one ticket, and dies. No warm pool, no state reuse, no surviving context. This eliminates state-leakage bugs between tickets. MTap is configurable per tier (`mtap: bool`); the leaf defaults on, routing tiers default off.

### Worker name pools

Code-defined constants in `madhu/names.py`. Seven pools:

| Pool | Members | Capacity | v0 assignment |
|---|---|---|---|
| `HEROES` | Rama, Yudhishthira, Arjuna, Lakshmana, Bhima, Nakula, Sahadeva, Bharata, Shatrughna, Hanuman | 10 | unused |
| `GRAHA` | Surya, Chandra, Brihaspati, Budha, Shukra, Mangala, Shani, Rahu, Ketu | 9 | unused |
| `GUARDIANS` | Indra, Varuna, Yama, Agni, Vayu, Kubera, Ishana, Nirriti | 8 | unused |
| `RISHIS` | Sanaka, Sananda, Sanatana, Vasishtha, Vishwamitra, Agastya, Atri, Bharadwaja | 8 | **Hamsa tier** |
| `PEETHAS` | Meru, Kailash, Mandara, Himalayas, Varanasi, Ujjain, Ayodhya, Vindhya | 8 | unused |
| `VAHANAS` | Garuda, Nandi, Hamsa, Makara, Simha, Vyaghra, Vrishabha, Mushika | 8 | unused |

`Hamsa` appears in both `KRISHNAS` (as the leaf tier) and `VAHANAS` (as the mount of Saraswati, who separates essence from non-essence). The double meaning is intentional: discerning essence from a raw model dump is the leaf worker's job.

### The lowercase-leaf rule

When the naming service generates a worker name, it lowercases the result if the worker's tier is the *deepest currently active* tier. For v0, Hamsa is leaf, so Hamsa workers display as `vasishtha`, `agastya`. The rule fires on whichever tier is leaf-of-the-moment.

### Lineage paths

A display-and-trace identifier carried by each spawned worker. Format: `{Xx}{Xx}…-{agent-name}`, where each `Xx` is the first two letters of an ancestor tier name (first word only for hyphenated tiers like `Nara-Narayana` → `Na`). For v0: Adi Purusha → Hamsa yields `AdHa-vasishtha`. The path is an agent-level identifier visible in the dashboard, JSONL events, and markdown; it is **not** a field on the ticket envelope.

---

## 4. System architecture

```
  External orchestrator (Claude Code / Opus)
            │  internally: param-aatma
            │
            │  MCP over stdio (synchronous)
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

### Core services

- **Ticket Store** — SQLite plus markdown sync. CRUD with migrate-on-read.
- **Touch Manager** — atomic acquire / release / forward.
- **Scheduler** — polls the queue, spawns workers per tier, enforces `max_parallel`, assigns lineage paths, and handles resilience (see §8).
- **Tier Registry** — loads tier configs from YAML; computes the deepest-active tier for the lowercase rule; validates each config, including that `provider` is a registered key.
- **Naming Service** — code-pinned pools, per-tier assignment, collision check against SQLite, lowercase-leaf rule, raises on pool exhaustion.
- **Migration Runner** — schema upgrades applied on read.
- **Provider layer** — swappable LLM backends behind a single `generate()` method.
- **Observability** — JSONL run log, per-ticket touch history, markdown sync, terminal dashboard.

---

## 5. The ticket

### Universal schema (v1.0)

```json
{
  "envelope": {
    "id": "uuid",
    "parent_id": "uuid | null",
    "forwarded_from": "uuid | null",
    "schema_version": "1.0",

    "tier_name": "Hamsa",
    "tier_level": 2,

    "status": "queued | touched | in_progress | done | failed | killed | forwarded | aborted",
    "collaboration_mode": "solo",
    "mtap": true,

    "created_at": "ISO-8601",
    "updated_at": "ISO-8601",
    "created_by_agent": "param-aatma",
    "assigned_to_agent": "agent-id | null",
    "touched_by": "agent-id | null",

    "touch_history": [
      { "agent": "vasishtha", "started": "ISO-8601", "ended": "ISO-8601", "summary": "wrote function, tests pass" }
    ],

    "failure_notes": [
      { "ticket_id": "uuid", "agent": "agastya", "failed_at": "ISO-8601", "reason": "...", "raw_excerpt": "first 500 chars of bad output" }
    ]
  },

  "payload": {
    "schema_version": "1.0",
    "type": "function_spec | task_brief | ...",
    "...": "tier-specific fields"
  },

  "result": {
    "status": "success | failure",
    "data": "...",
    "produced_at": "ISO-8601",
    "by_agent": "agent-id"
  }
}
```

### Status enum

| Status | Meaning |
|---|---|
| `queued` | Awaiting assignment |
| `touched` | Acquired by an agent, not yet started |
| `in_progress` | Agent actively working |
| `done` | Successful terminal state |
| `failed` | Worker reported failure (may be forwarded) |
| `killed` | Externally terminated — operator or timeout |
| `forwarded` | Superseded by a new ticket with appended `failure_notes` |
| `aborted` | Internally terminated — forwarding limit exceeded |

`killed` and `aborted` are distinct: `killed` is external termination; `aborted` is internal termination when a forward chain exceeds `max_forwards`.

### Per-tier payloads

- **Hamsa (`function_spec`):** `function_name`, `signature`, `docstring`, `constraints` (list), `examples` (non-empty list of input/output dicts), `imports_allowed` (list). Validators enforce a valid identifier, that the signature contains the function name, that examples are non-empty, and that constraints is a list.
- **Adi Purusha:** receives no payloads; it only creates tickets.

---

## 6. Failure handling

When a worker fails, the ticket is killed and a new ticket is created. The new ticket copies the prior `failure_notes`, appends a fresh entry (failing agent, reason, raw excerpt), sets the old ticket's status to `forwarded`, and sets its own `forwarded_from` to the old ticket's id. A different worker picks it up.

Each tier's `failure_policy` sets `max_forwards` (Hamsa default: 3) and `on_max` (default: `abort`). When the forward count exceeds `max_forwards`, the scheduler sets the ticket to `aborted`. There are no silent retries; every attempt is recorded.

---

## 7. Provider abstraction

The worker is provider-agnostic. Providers implement a single contract:

```
generate(prompt: str, model: str, temperature: float, timeout: float) -> str
```

A provider returns raw model output. Response cleaning — channel-marker stripping, code-fence stripping, AST validation that the output is exactly one function definition — happens in the worker (`madhu/workers/hamsa.py`), never in the provider. Providers are dumb output pipes; they never see tickets, envelopes, or payloads.

```
madhu/workers/
├── base.py          Provider protocol + ProviderError
├── hamsa.py         run_worker() entry point; all response cleaning
└── providers/
    ├── __init__.py  PROVIDER_REGISTRY = {"ollama": OllamaProvider}
    └── ollama.py    OllamaProvider — httpx POST to /api/generate
```

The tier config selects the provider and supplies its construction args:

```yaml
provider: "ollama"
provider_config:
  model: "hf.co/yuxinlu1/gemma-4-12B-coder-fable5-composer2.5-v1-GGUF:Q8_0"
  endpoint: "http://localhost:11434"
  temperature: 0.2
  timeout: 120
```

At spawn, the worker reads the config, looks up the provider class in `PROVIDER_REGISTRY`, instantiates it, and calls `generate(...)`. v0 ships one concrete provider (Ollama). The registry is explicit — no plugin auto-discovery, no entry-point magic.

---

## 8. Scheduler

The scheduler polls SQLite for `queued` tickets, looks up the tier config, enforces `max_parallel`, and spawns one `multiprocessing.Process` per ticket. It tracks live workers internally and assigns each its lineage path at spawn. Worker entry points are module-level functions (picklable across the spawn boundary).

Resilience behaviour:

- **Janitor (startup only).** On `run()`, before the poll loop, the janitor scans for tickets left `touched` or `in_progress` with no live worker — orphans from a prior crash — and forwards each with an orphaned-by-restart note. Runs once at startup, never mid-loop.
- **Worker wall-clock timeout.** Each tier config carries `worker_timeout_seconds` (Hamsa default: 180). Workers exceeding it are killed and the ticket is forwarded as a normal failure.
- **Graceful shutdown (SIGINT).** The loop stops accepting new tickets; in-flight workers get a grace period to release their touch; survivors are terminated and any still `in_progress` are set to `killed`.

---

## 9. MCP surface

`server.py` exposes MadCP over MCP stdio. The scheduler runs in a background thread started at server boot. All logging goes to stderr; stdout is reserved for the MCP protocol.

| Tool | Behaviour |
|---|---|
| `submit_ticket(envelope, payload)` | Validates via Pydantic, stamps `created_by_agent="param-aatma"` if unset, inserts as `queued`, polls until a terminal state, returns the full ticket including result. Internal timeout default 600s; on timeout returns current state without killing. |
| `list_tickets(filter)` | Forwards to `store.list(...)`. |
| `check_ticket(id)` | Returns `store.read(id)`. |
| `health_check()` | Returns server status, scheduler liveness, queue depth, in-progress count, active tiers, last terminal timestamp. Non-blocking. |

Tool descriptions in the server are extensive and include inline example payloads, since the orchestrator reads them to decide how to call the surface.

---

## 10. Persistence and observability

- **SQLite** — two tables, `tickets` and `touch_history`. The status column accepts all eight enum values. Connection uses `check_same_thread=False` with a lock. All reads run migrations before deserializing.
- **Markdown sync** — one `.md` file per ticket in `tickets/`, named `{id}.md`. YAML frontmatter carries the envelope; the body carries payload, touch history with `[[wiki-links]]`, failure notes, and result. Written after every store create/update. Obsidian-friendly.
- **JSONL run log** — `logs/runs.jsonl`, append-only, flushed per write. One JSON object per event: `timestamp`, `event_type`, `ticket_id`, `agent_name` (lineage path), `details`. Event types include `worker_spawn`, `worker_exit`, `touch_acquire`, `touch_release`, `forward`, `ollama_call`, `ollama_result`, `mcp_submit_enter`, `mcp_submit_exit`.
- **Terminal dashboard** — `dashboard.py`, a separate process, `rich`-based, read-only on SQLite, refreshing at 1 Hz. Shows tier counts, live agents with lineage paths and elapsed time, and recent tickets. `aborted` is rendered distinctly from `killed`. Key bindings: `q` quit, `r` refresh, `f` filter, `t` tail logs.

---

## 11. Repository layout

```
madhu/                                repo root
├── pyproject.toml                    name = "madhu"
├── README.md
├── LICENSE
├── MadCP.md                          this document
├── CLAUDE.md
├── server.py                         MCP entry point
├── dashboard.py                      TUI entry point
│
├── madhu/                            package
│   ├── __init__.py
│   ├── names.py                      KRISHNAS + worker pools
│   ├── naming.py                     naming service
│   ├── scheduler.py                  scheduler + lineage paths
│   ├── schemas/
│   │   ├── envelope.py               Ticket, Envelope, Result, KRISHNAS
│   │   ├── payloads.py               FunctionSpec
│   │   └── migrations/               append-only, applied on read
│   ├── store/
│   │   ├── sqlite.py                 ticket store
│   │   ├── markdown.py               markdown sync
│   │   └── touch.py                  touch manager
│   ├── tiers/
│   │   ├── registry.py               loads tier configs
│   │   └── configs/
│   │       ├── adi_purusha.yaml
│   │       └── hamsa.yaml
│   ├── workers/
│   │   ├── base.py                   Provider protocol
│   │   ├── hamsa.py                  run_worker entry point
│   │   └── providers/
│   │       ├── __init__.py           PROVIDER_REGISTRY
│   │       └── ollama.py             OllamaProvider
│   └── observability/
│       ├── jsonl.py                  run log
│       └── dashboard_data.py         TUI data source
│
├── tiers/configs/                    (active tier YAMLs)
├── tickets/                          markdown sync output
├── data/                             SQLite database
├── logs/                             runs.jsonl
└── tests/
```

---

## 12. Technology

- **Language:** Python 3.11+
- **MCP SDK:** official `mcp` package
- **Schema validation:** Pydantic v2 (`ConfigDict`-style)
- **Storage:** SQLite (stdlib `sqlite3`)
- **HTTP client:** `httpx`
- **TUI:** `rich`
- **Process management:** `multiprocessing` (spawn-fresh workers)
- **Testing:** `pytest`, `pytest-asyncio`, `respx`

---

## 13. Glossary

| Term | Meaning |
|---|---|
| **MadCP** | Madhu Context Protocol — the public server name |
| **madhu** | Mediated Agent Delegation & Handoff Utility — the package/engine |
| **param-aatma** | Internal name for the external orchestrator |
| **KRISHNAS** | The 24-name tier list |
| **Adi Purusha** | Tier 1, top — routes, does not execute |
| **Hamsa** | Leaf tier — executes via local model |
| **MTap** | Leaf workers are ephemeral by default |
| **Touch** | Exclusive single-agent claim on a ticket |
| **Lineage path** | `{Xx}{Xx}-{name}` trace identifier for a spawned worker |
| **Forward** | Kill a failed ticket; create a successor carrying accumulated failure notes |
| **aborted** | Terminal status when a forward chain exceeds `max_forwards` |

---

*End of public architecture document (v2.2).*
