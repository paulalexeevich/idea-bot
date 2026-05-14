# todo-bot — CLAUDE.md

## ToDo System — Repository Map

This repo is part of the **ToDo** personal productivity system. All repositories:

| Repo | Type | Status | Description |
|------|------|--------|-------------|
| [paulalexeevich/todo-bot](https://github.com/paulalexeevich/todo-bot) | UI + Core | current repo | Telegram bot, Next.js dashboard (`dashboard/`), data-api (`data-api/`) — **live system** |
| [paulalexeevich/todo-api](https://github.com/paulalexeevich/todo-api) | Core | planned migration | Future standalone core (not yet live) |
| [paulalexeevich/discovery-agent](https://github.com/paulalexeevich/discovery-agent) | Agent | planned | Standalone idea validation pipeline |
| [paulalexeevich/buyer-agent](https://github.com/paulalexeevich/buyer-agent) | Agent | planned | Standalone product search pipeline |

> **Note for agents**: The live data API is `data-api/` in this repo, running as a Docker service on the VPS. The `todo-api` repo is a planned future migration — it has no live data.

---


A personal Telegram bot that classifies messages, manages tasks/reminders, validates startup ideas, finds products to buy, and builds a long-term knowledge graph of the user from their conversations.

**Detailed design:** see [docs/architecture.md](docs/architecture.md).  
**System flow diagrams:** see [docs/flow.md](docs/flow.md).

---

## Keeping docs up to date

When making any non-trivial change, update docs in the same commit:

- New task type, command, or routing branch → update tables in `CLAUDE.md` + routing section in `docs/architecture.md`
- New DB column or table → update schema in `docs/architecture.md`
- New data-api endpoint → update endpoint table in `docs/architecture.md`
- New agent, LangGraph node, or job → update relevant pipeline section in `docs/architecture.md`
- New env var → add to table in `CLAUDE.md` and to `.env.example`
- LLM model change → update model table in `docs/architecture.md`

---

## Project structure

```
idea-bot/
├── CLAUDE.md
├── docs/
│   └── architecture.md         # full design reference
├── .env.example
├── .env                        # gitignored — actual secrets
├── docker-compose.yml          # 5 services: idea-bot, data-api, memory-agent, neo4j, dashboard
├── Dockerfile                  # idea-bot container
├── pyproject.toml
├── main.py                     # entry point — registers handlers + all scheduled jobs
├── config.py                   # pydantic-settings; loads .env once
│
├── db/
│   ├── models.py               # Task, Discovery, Source, Offer dataclasses
│   ├── client.py               # HTTP client → data-api (ALL bot DB access goes here)
│   └── database.py             # DEAD CODE — old direct-SQLite layer, do not use
│
├── bot/
│   ├── handlers/
│   │   ├── idea.py             # MessageHandler: save → classify+memory → route
│   │   └── commands.py         # /list /report /status /debug_run /location /sethome /setlocation /timezone /settimezone /reminders
│   ├── jobs/
│   │   ├── discovery.py        # nightly: pending ideas → discovery pipeline → notify
│   │   ├── buyer.py            # immediate: shopping task → buyer pipeline → notify
│   │   ├── reminders.py        # every 60s: fire due reminders
│   │   ├── notifier.py         # every 60s: notify newly-completed tasks
│   │   └── memory.py           # session idle (Tier 2) + daily reflection (Tier 3)
│   └── integrations/
│       └── github.py           # save architecture/learning tasks as GitHub issues
│
├── agent/
│   ├── classifier.py           # structured-output LLM classifier; injects short + long-term memory
│   ├── task_agent.py           # tool-calling agent: save_reminder/save_task/ask_clarification + MCP memory
│   ├── query_agent.py          # read-only tool-calling agent for retrieval queries; never saves tasks
│   ├── time_parser.py          # regex-based HH:MM parser (no LLM needed)
│   ├── deadline.py             # LLM date parser → DeadlineInfo (for shopping deadlines)
│   ├── graph.py                # LangGraph discovery pipeline
│   ├── buyer_graph.py          # LangGraph buyer pipeline
│   ├── state.py                # DiscoveryState TypedDict
│   └── nodes/
│       ├── reddit.py, hackernews.py, producthunt.py, indiehackers.py
│       ├── synthesize.py       # LLM synthesis of research sources → DiscoveryResult
│       └── buyer.py            # DuckDuckGo search + delivery estimation
│
├── data-api/
│   ├── main.py                 # FastAPI app — all REST endpoints
│   └── database.py             # aiosqlite helpers + init_db() with inline migrations
│
├── memory-agent/
│   ├── main.py                 # FastAPI app — /health, /memory/process-now, /memory/process-session, /memory/reflect
│   ├── extractor.py            # LLM graph extraction (3 tiers: exchange, session, reflection)
│   ├── graph_client.py         # Neo4j async client — merge, query, reflect
│   └── mcp_server.py           # MCP server: query_memory, save_memory, list_entities
│
├── dashboard/                  # Next.js + shadcn/base-ui task browser (port 3000)
│
└── tests/
    ├── conftest.py             # sets minimal env vars so tests run outside project dir
    ├── test_db.py              # db/client.py tests via respx mocks
    ├── test_nodes.py           # agent node tests (mocked HTTP)
    ├── test_pipeline.py        # discovery graph state flow
    └── test_task_agent_mock.py # task_agent tests
```

---

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `TELEGRAM_BOT_TOKEN` | yes | Bot token from @BotFather |
| `TELEGRAM_USER_ID` | yes | Your numeric Telegram user ID |
| `LLM_PROVIDER` | yes | `gemini` (default) \| `claude` \| `openai` |
| `GOOGLE_GEMINI_API_KEY` | if gemini | Gemini API key |
| `ANTHROPIC_API_KEY` | if claude | Anthropic API key |
| `OPENAI_API_KEY` | if openai | OpenAI API key |
| `REDDIT_CLIENT_ID` | optional | PRAW OAuth |
| `REDDIT_CLIENT_SECRET` | optional | PRAW OAuth |
| `PRODUCT_HUNT_TOKEN` | optional | PH GraphQL API |
| `DATA_API_URL` | yes | `http://data-api:8001` in Docker |
| `DATA_API_KEY` | yes | Shared secret for `X-API-Key` header |
| `MEMORY_AGENT_URL` | yes | `http://memory-agent:8002` in Docker |
| `GITHUB_TOKEN` | optional | `issues:write` scope — for arch/learning tasks |
| `GITHUB_REPO` | optional | `owner/repo` |
| `HOME_LOCATION` | optional | Default home city (e.g. `Moscow, Russia`) |
| `DISCOVERY_HOUR` | optional | UTC hour for nightly discovery (default: `2`) |
| `DISCOVERY_MINUTE` | optional | UTC minute (default: `0`) |
| `NEO4J_PASSWORD` | yes | Neo4j password (used by memory-agent) |

---

## Telegram commands

| Command / Input | Behaviour |
|----------------|-----------|
| Any free text | Saved instantly, classified with memory context, routed by type |
| `/list` | Last 10 tasks with status + type emoji |
| `/report <id>` | Full discovery report: score, verdict, market size, competitors |
| `/status` | Task counts by status + next discovery run time |
| `/debug_run` | Trigger nightly discovery immediately |
| `/location` | Show home + current location |
| `/setlocation <city>` | Update current location (used for local shopping) |
| `/sethome <city>` | Update home location |
| `/timezone` | Show current timezone setting |
| `/settimezone <tz>` | Set timezone (IANA name, e.g. `Europe/Budapest`); validated + persisted to settings table |
| `/reminders` | List upcoming pending reminders with local time conversion |

---

## Key conventions

- **All bot DB access via `db/client.py`** — HTTP client to data-api. Never import from `data-api/` in bot code.
- **`db/database.py` is dead code** — old direct-SQLite layer from before the data-api existed. Do not use.
- **All async** — `httpx.AsyncClient`, `asyncio`. Never `requests` or `time.sleep`.
- **Instant reply, async classify** — `handle_message` saves the task and replies in ~50ms. Classification + memory query run in `asyncio.create_task`.
- **Both messages and bot replies are saved** — every user message and every bot reply is written to the `messages` table via `save_message()`. This feeds the memory extraction pipeline. This now includes bot replies from all reminder clarification handlers (`_handle_reminder_date_reply`, `_handle_reminder_time_reply`) and the shopping deadline handler (`_handle_deadline_reply`) — previously these handlers replied but never saved to the messages table or triggered Tier 1 extraction (bug fixed).
- **Single-user guard** — every handler checks `update.effective_user.id == settings.telegram_user_id`.
- **Jobs via PTB job_queue** — `run_daily` / `run_repeating` registered in `main.py`. No system cron.
- **Memory agent is optional** — if `MEMORY_AGENT_URL` is empty, the bot works without long-term memory; short-term context (last 20 messages) still flows into classification.
- **Query routing bypasses task creation** — `handle_message` applies `_QUERY_RE` (regex) before the normal save path. If it matches and no AWAITING state is active, the message is classified inline; if the type is `query`, `_handle_query` runs the query agent and returns immediately — no task row is created, no `create_task` call is made. The `query` type is also handled as a fallback in `_classify_and_followup` in case the regex misses it.

---

## Running with Docker (recommended)

```bash
cp .env.example .env   # fill in secrets
docker compose up -d
docker compose logs -f idea-bot
```

## Running locally (two terminals)

```bash
# Terminal 1 — data-api
cd data-api
DB_PATH=../data/tasks.db DATA_API_KEY=dev-key uvicorn main:app --port 8001

# Terminal 2 — bot (memory-agent optional)
DATA_API_URL=http://localhost:8001 DATA_API_KEY=dev-key MEMORY_AGENT_URL="" \
  python main.py
```

---

## Testing

```bash
python -m pytest tests/ -v                    # from any directory
python -m pytest tests/test_nodes.py -v       # node unit tests (mocked HTTP)
python -m pytest tests/test_pipeline.py -v    # LangGraph state flow
python -m pytest tests/test_db.py -v          # db/client HTTP layer
```

---

## Adding a new task type

1. Add to `TASK_TYPES` dict in `agent/classifier.py`
2. Add routing branch in `_classify_and_followup` in `bot/handlers/idea.py`
3. Add emoji to `_TYPE_EMOJI` in the same file
4. If it needs a new job: add to `bot/jobs/`, register in `main.py`
5. If it needs new DB columns: add `ALTER TABLE` migration in `data-api/database.py:init_db()`

## Deploying to VPS

```bash
# Sync source (never overwrite .env or data/)
rsync -av --exclude='.git/' --exclude='data/' --exclude='.env' \
  --exclude='.venv/' --exclude='__pycache__/' --exclude='*.egg-info/' \
  /Users/pavelp/idea-bot/ hetzner:/opt/agents/idea-bot/

# Rebuild and restart changed services
ssh hetzner "cd /opt/agents/idea-bot && docker compose up -d --build idea-bot data-api"
```
