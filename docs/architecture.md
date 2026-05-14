# Architecture

## Service topology

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Docker Compose (Hetzner VPS 204.168.187.216)                           │
│                                                                         │
│  ┌─────────────────┐   HTTP/X-API-Key   ┌──────────────────────────┐   │
│  │   idea-bot       │ ────────────────▶ │   data-api (FastAPI)     │   │
│  │  (PTB polling)  │                    │   port 8001              │   │
│  └────────┬────────┘                    └──────────┬───────────────┘   │
│           │                                        │ aiosqlite          │
│           │ HTTP POST                     ┌────────▼────────────────┐  │
│           │ /memory/*                     │   SQLite (tasks.db)     │  │
│           │ MCP (langchain-mcp-adapters)  │   Docker volume         │  │
│           ▼                               └─────────────────────────┘  │
│  ┌─────────────────┐   bolt://           ┌──────────────────────────┐  │
│  │  memory-agent   │ ────────────────▶   │   Neo4j 5               │  │
│  │  (FastAPI)      │                     │   ports 7474/7687        │  │
│  │  port 8002      │                     │   Docker volume          │  │
│  │  MCP at /mcp    │                     └──────────────────────────┘  │
│  └─────────────────┘                                                    │
│                                                                         │
│  ┌─────────────────┐   HTTP              ┌──────────────────────────┐  │
│  │   dashboard      │ ────────────────▶  │   data-api (same)        │  │
│  │  (Next.js)      │                     └──────────────────────────┘  │
│  │  port 3000      │                                                    │
│  └─────────────────┘                                                    │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Full message flow

Every incoming Telegram message follows this path:

```
User sends message
      │
      ├─ save_message("user", text)          → messages table, processed=0
      ├─ set_setting("last_user_message_at") → for Tier 2 idle detection
      │
      ├─ Check AWAITING_REMINDER_TASK_KEY    → _handle_reminder_date/time_reply → save_message + POST /memory/process-now (Tier 1)
      ├─ Check AWAITING_TASK_KEY             → _handle_deadline_reply (shopping) → save_message + POST /memory/process-now (Tier 1)
      │
      └─ create_task(text, type="note")      → instant reply "Task #N saved ✓"
             │
             └─ asyncio.create_task(_classify_and_followup)
                        │
                        ├─ get_recent_messages(20)           short-term context
                        ├─ MCP query_memory(text)            long-term context (Neo4j)
                        │
                        ├─ classify_task(text, context, long_term_context)
                        │         structured output: type, title, due_date, due_time,
                        │         search_query, location
                        │
                        ├─ set_task_type(task_id, type)
                        │
                        ├─ [idea]         → queue for nightly discovery
                        ├─ [shopping]     → ask deadline → run_buyer (immediate)
                        ├─ [reminder]     → parse due_date+due_time → update_task_reminder
                        │                   or ask for missing date/time
                        │                   (date reply: parse_reminder_datetime LLM extracts date+time
                        │                    together in one call — if user says "tomorrow at 10" both
                        │                    are resolved immediately with no extra question needed)
                        ├─ [architecture] → save to GitHub issue
                        ├─ [learning]     → save to GitHub issue
                        └─ [todo|note|question|other] → emoji reply
                                   │
                        bot reply → save_message("bot", reply)
                                  → POST /memory/process-now   (Tier 1)
```

---

## Message routing — task types

| Type | Trigger rules | Action |
|------|--------------|--------|
| `reminder` | "remind me", "alert at", "don't forget" + time/date | Set due_date + due_time; ask for missing piece |
| `shopping` | "buy", "find", "search for" a product | Ask deadline → buyer pipeline |
| `idea` | Startup / product concept | Queue for nightly discovery |
| `todo` | Actionable verb, no specific time | Save + emoji reply |
| `architecture` | Technical design decision | Save + GitHub issue |
| `learning` | Lesson learned, insight | Save + GitHub issue |
| `question` | Open question to think through | Save + emoji reply |
| `note` | Anything else — link, fact, reference | Save + emoji reply (default) |

---

## Database schema

All tables in `data-api/database.py:init_db()`. Inline `ALTER TABLE` migrations run on every startup.

```sql
CREATE TABLE tasks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    text                TEXT NOT NULL,
    type                TEXT NOT NULL DEFAULT 'idea',
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status              TEXT DEFAULT 'pending',    -- pending | processing | done | error
    -- shopping
    deadline            TEXT,                      -- ISO date (urgency deadline)
    urgency             TEXT,                      -- asap | fast | week | flexible | any
    -- reminders
    due_date            TEXT,                      -- ISO date YYYY-MM-DD
    due_time            TEXT,                      -- 24h HH:MM
    notified_at         TEXT,                      -- ISO datetime when reminder was sent
    -- completion tracking
    completed_notified  INTEGER DEFAULT 0          -- 1 after done notification sent
);

CREATE TABLE messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role        TEXT NOT NULL,          -- "user" or "bot"
    content     TEXT NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed   INTEGER DEFAULT 0      -- 0 = not yet extracted into Neo4j
);

CREATE TABLE discoveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER NOT NULL REFERENCES tasks(id),
    ran_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reddit_summary  TEXT,
    hn_summary      TEXT,
    ph_summary      TEXT,
    ih_summary      TEXT,
    verdict         TEXT,
    score           REAL,              -- 0.0–10.0
    market_size     TEXT,
    full_report     TEXT               -- JSON: {competitors, sentiment_summary, sources[]}
);

CREATE TABLE offers (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id                 INTEGER NOT NULL REFERENCES tasks(id),
    title                   TEXT NOT NULL,
    price                   TEXT,
    store                   TEXT,
    url                     TEXT NOT NULL,
    snippet                 TEXT,
    location_context        TEXT,      -- local | online | any
    delivery_days_estimate  INTEGER,
    found_at                TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
    -- keys used: home_location, current_location, last_user_message_at,
    --            session_extracted_at, awaiting_task_id, awaiting_search_query,
    --            awaiting_location_type, awaiting_reminder_task_id,
    --            awaiting_reminder_date, awaiting_reminder_time,
    --            user_timezone
);
```

---

## Memory system (3 tiers)

All memory is stored in Neo4j as a knowledge graph of entities and relationships extracted from conversations.

### Tier 1 — Per exchange (immediate)
**Trigger:** After every bot reply (called directly from `_classify_and_followup`)
**What:** `POST /memory/process-now` → fetch `messages` WHERE `processed=0` → `extract_graph()` → merge nodes/edges into Neo4j → mark messages processed
**Extracts:** Explicit facts only: people mentioned, preferences stated, events referenced

### Tier 2 — Session patterns (idle)
**Trigger:** `check_session_idle` job (every 60s); fires when user has been quiet for 10 min and session not yet extracted
**What:** `POST /memory/process-session` → fetch last 30 messages → `extract_session()` → merge into Neo4j
**Extracts:** Patterns across the full session: recurring themes, implied routines, clusters of related tasks

### Tier 3 — Daily reflection (03:00 UTC)
**Trigger:** `daily_reflection` job (daily at 03:00 UTC)
**What:** `POST /memory/reflect` → fetch full graph summary + last 100 messages → `reflect_on_graph()` → merge duplicates, add new patterns, prune stale nodes
**Extracts:** Graph quality improvements: deduplication, new cross-session patterns, stale node removal

### Neo4j entity types
`Person | Preference | RecurringEvent | Place | Topic`

### Neo4j relationship types
`KNOWS | HAS_PREFERENCE | ATTENDS | LOCATED_AT | INTERESTED_IN | RELATED_TO`

---

## MCP interface (memory-agent at /mcp)

The memory-agent exposes an MCP server using `FastMCP`. These tools are consumed by the bot via `langchain-mcp-adapters` during classification.

| Tool | Description |
|------|-------------|
| `query_memory(query)` | Search Neo4j for entities/facts matching the query. Returns formatted context string injected into classifier prompt. |
| `save_memory(fact)` | Extract graph data from a plaintext fact and merge into Neo4j. Called when the bot learns something lasting. |
| `list_entities(entity_type?)` | Browse the knowledge graph. Optional filter by type. |

---

## Classification — memory injection

`agent/classifier.py:classify_task()` receives two memory contexts:

```
short_term: last 20 messages from messages table
long_term:  Neo4j context string from query_memory(text)
```

Both are injected into the system prompt before the structured output call:

```
What you know about this user:
- Person 'Olga': relation_to_user=wife
  → KNOWS [Person] User

Recent conversation:
user: Remind me to call Olga at 9pm
bot: → ⏰ reminder
```

Uses `with_structured_output(_ClassifyOutput)` — typed Pydantic model, no JSON parsing fragility.

---

## Task agent (`agent/task_agent.py`)

A full tool-calling agent loop that serves as the more capable alternative to `classify_task`. Not yet wired into the main message handler — currently used via CLI (`python agent/task_agent.py "task text"`).

### Tools available to the LLM
| Tool | Description |
|------|-------------|
| `save_reminder` | Create task + set due_date + due_time |
| `save_task` | Create task of any non-reminder type |
| `ask_clarification` | Ask user a specific question (for missing date/time) |
| `query_memory` | MCP: search knowledge graph (if `MEMORY_AGENT_URL` set) |
| `save_memory` | MCP: store new long-term fact |
| `list_entities` | MCP: browse knowledge graph |

The agent loops up to 8 rounds: calls MCP tools for context, then calls exactly one task tool.

**Redesign opportunity:** `task_agent.process_task()` could replace `classify_task()` in the main message handler for richer, memory-aware task processing.

---

## LangGraph pipelines

### Discovery pipeline (`agent/graph.py`)

Validates startup ideas nightly. All 4 research nodes run in parallel.

```
START ──┬──▶ reddit_node        (PRAW)
        ├──▶ hackernews_node    (Algolia HN Search API)
        ├──▶ producthunt_node   (PH GraphQL API v2)
        └──▶ indiehackers_node  (httpx + BeautifulSoup)
                   └────────────────────┘
                             ▼
                       synthesize_node (LLM)
                             ▼
                            END
```

### Buyer pipeline (`agent/buyer_graph.py`)

Finds purchase options immediately after deadline is captured. Single node using DuckDuckGo + delivery estimation.

---

## Scheduled jobs (`main.py`)

| Job | Schedule | Module |
|-----|----------|--------|
| `run_discovery` | Daily at `DISCOVERY_HOUR:DISCOVERY_MINUTE` UTC | `bot/jobs/discovery.py` |
| `check_reminders` | Every 60s (first=10s) | `bot/jobs/reminders.py` |
| `check_completions` | Every 60s (first=15s) | `bot/jobs/notifier.py` |
| `check_session_idle` | Every 60s (first=30s) | `bot/jobs/memory.py` |
| `daily_reflection` | Daily at 03:00 UTC | `bot/jobs/memory.py` |

All registered via PTB's `application.job_queue`. No system cron.

---

## data-api endpoints

All require `X-API-Key` header. Base URL: `DATA_API_URL` (default `http://data-api:8001`).

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness (no auth) |
| POST | `/tasks` | Create task |
| GET | `/tasks` | List tasks (`status`, `type`, `limit` filters) |
| GET | `/tasks/{id}` | Get task + discovery |
| PATCH | `/tasks/{id}/status` | Update status |
| PATCH | `/tasks/{id}/type` | Update type |
| PATCH | `/tasks/{id}/deadline` | Set shopping deadline + urgency |
| PATCH | `/tasks/{id}/reminder` | Set `due_date` + `due_time` |
| POST | `/tasks/{id}/notified` | Mark reminder notified (sets `notified_at`) |
| POST | `/tasks/{id}/completion-notified` | Mark done notification sent |
| POST | `/tasks/{id}/discovery` | Save discovery result |
| GET | `/tasks/{id}/discovery` | Get latest discovery |
| POST | `/tasks/{id}/offers` | Save offer |
| GET | `/tasks/{id}/offers` | List offers |
| GET | `/tasks/done/new` | Tasks just marked done, not yet notified |
| GET | `/reminders/due` | Pending reminders with `due_date+due_time <= ?now` |
| POST | `/messages` | Save message (`role`, `content`) |
| GET | `/messages/recent` | Last N messages |
| GET | `/messages/unprocessed` | Messages with `processed=0` |
| POST | `/messages/processed` | Mark message IDs as processed |
| GET | `/settings/{key}` | Get setting value |
| PUT | `/settings/{key}` | Upsert setting value |
| GET | `/counts` | Task counts grouped by status |

---

## LLM model choices

| Use case | Gemini | Claude | OpenAI |
|----------|--------|--------|--------|
| Classification (`classifier.py`) | `gemini-3.1-flash-lite` | `claude-haiku-4-5-20251001` | `gpt-4o-mini` |
| Deadline parsing (`deadline.py`) | `gemini-3.1-flash-lite` | `claude-sonnet-4-6` | `gpt-4o` |
| Reminder datetime parsing (`deadline.py:parse_reminder_datetime`) | `gemini-3.1-flash-lite` | `claude-sonnet-4-6` | `gpt-4o` |
| Task agent (`task_agent.py`) | `gemini-2.5-flash` | `claude-haiku-4-5-20251001` | `gpt-4o-mini` |
| Memory extraction (`extractor.py`) | `gemini-2.5-flash` | `claude-haiku-4-5-20251001` | `gpt-4o-mini` |
| Discovery synthesis (`synthesize.py`) | `gemini-2.5-pro` | `claude-sonnet-4-6` | `gpt-4o` |

---

## Multi-turn conversation state

Stored as key-value pairs in the `settings` table. Only one two-step flow can be active at a time (single-user bot).

| Key | Flow |
|-----|------|
| `awaiting_task_id` | Shopping: waiting for deadline reply |
| `awaiting_search_query` | Shopping: stored while waiting |
| `awaiting_location_type` | Shopping: stored while waiting |
| `awaiting_reminder_task_id` | Reminder: date or time still needed |
| `awaiting_reminder_date` | Reminder: `NEEDED` or stored ISO date |
| `awaiting_reminder_time` | Reminder: `NEEDED` or stored HH:MM |
| `user_timezone` | User's IANA timezone name (e.g. `Europe/Budapest`). Default: `UTC`. Used when formatting reminder confirmations and notifications. |

---

## Dashboard (`dashboard/`)

Next.js app using `base-ui/react`, shadcn, Tailwind. Served on port 3000. Reads from `data-api` via `DATA_API_URL`. Provides a visual task browser for reviewing saved ideas, discovery reports, and offers.
