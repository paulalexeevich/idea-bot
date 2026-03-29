# Idea Bot — CLAUDE.md

## Project Overview

A personal Telegram bot that captures any kind of thought — startup ideas, tasks, notes, learnings, architecture decisions, questions, and shopping requests — and routes each to the right automated pipeline:

- **Ideas** → nightly LangGraph discovery pipeline (Reddit, HN, Product Hunt, IndieHackers → LLM synthesis)
- **Shopping** → immediate DuckDuckGo search, location-aware, deadline-aware, returns ranked offers
- **Architecture / Learning** → saved as markdown files to a GitHub knowledge repo
- **Todo / Note / Question** → saved and listed, no further automation

---

## Architecture

```
Telegram free-text → python-telegram-bot v20+ (async)
                           │
                    LLM Classifier (agent/classifier.py)
                           │
          ┌────────────────┼──────────────────┐
          ▼                ▼                  ▼
       idea             shopping         architecture
          │                │             learning
   nightly job      deadline prompt    GitHub API
   (discovery)             │           (bot/integrations/github.py)
          │          deadline parse
          ▼                │
  LangGraph Discovery   BuyerGraph
  Pipeline              (agent/buyer_graph.py)
  (agent/graph.py)           │
          │             DuckDuckGo search
          ▼                  │
  data-api HTTP client   data-api HTTP client
  (db/client.py)         (db/client.py)
          │
  data-api FastAPI service (data-api/)
          │
       SQLite
```

The bot never accesses SQLite directly. All persistence goes through the `data-api` service via `db/client.py` (an async `httpx` client).

---

## Key Design Decisions

- **Two-service Docker Compose**: `data-api` (FastAPI + SQLite, port 8001) and `idea-bot`. The bot depends on `data-api` being healthy before starting.
- **DB access via HTTP**: `db/client.py` wraps all DB operations as HTTP calls to `data-api`. This isolates persistence and allows independent scaling.
- **LLM is abstracted**: `config.LLM_PROVIDER` selects `gemini` (default), `claude`, or `openai`. Classification and deadline parsing each have their own `_call_llm()` helpers in `agent/classifier.py` and `agent/deadline.py`.
- **Message is saved first, classified second**: `handle_message` saves with type `note` and replies instantly. Classification runs in the background via `asyncio.create_task`.
- **Shopping flow is stateful**: After classifying as `shopping`, the bot asks for a deadline. The reply is caught via a `settings`-stored key (`awaiting_task_id`). Deadline is parsed by LLM (`agent/deadline.py`) into a strategy (`asap | fast | week | flexible | any`).
- **Single-user bot**: All handlers check `update.effective_user.id == settings.telegram_user_id`. Unknown users are silently ignored.
- **Nightly scheduling**: `bot/jobs/discovery.py` registered via `application.job_queue.run_daily()` in `main.py`. Do not use system cron or a separate process.
- **Session claiming on startup**: `post_init` calls `delete_webhook(drop_pending_updates=True)` + 2s sleep to prevent 409 Conflict on container restart.

---

## Project Structure

```
idea-bot/
├── CLAUDE.md               # this file
├── .env.example            # all required env vars documented
├── .env                    # gitignored — actual secrets
├── docker-compose.yml      # two services: data-api + idea-bot
├── Dockerfile              # idea-bot image
├── fly.toml                # Fly.io deployment config
├── pyproject.toml
├── main.py                 # entry point — registers handlers, jobs, starts polling
├── config.py               # pydantic-settings, loads .env once → settings singleton
├── db/
│   ├── client.py           # HTTP client to data-api (all DB access goes here)
│   ├── database.py         # legacy — original aiosqlite layer, not used by bot
│   └── models.py           # Task, Source, DiscoveryResult, Offer, Discovery dataclasses
├── agent/
│   ├── classifier.py       # LLM classifier → TaskClassification (type + title + search_query + location)
│   ├── deadline.py         # LLM deadline parser → DeadlineInfo (date, days_until, strategy)
│   ├── state.py            # DiscoveryState TypedDict
│   ├── graph.py            # Discovery LangGraph: 4 parallel research nodes → synthesize
│   ├── buyer_graph.py      # Buyer LangGraph: single buyer_node
│   └── nodes/
│       ├── reddit.py
│       ├── hackernews.py
│       ├── producthunt.py
│       ├── indiehackers.py
│       ├── synthesize.py
│       └── buyer.py        # DuckDuckGo search, location/deadline-aware, returns list[Offer]
├── bot/
│   ├── handlers/
│   │   ├── idea.py         # MessageHandler — classify → route + stateful deadline flow
│   │   └── commands.py     # /list /report /status /debug_run /location /setlocation /sethome
│   ├── integrations/
│   │   └── github.py       # saves architecture/learning as markdown to GitHub repo
│   └── jobs/
│       ├── discovery.py    # nightly batch: pending idea tasks → discovery pipeline → notify
│       └── buyer.py        # run_buyer() — invoke buyer_graph, save offers, notify
├── data-api/
│   ├── Dockerfile
│   ├── main.py             # FastAPI app — all REST endpoints
│   ├── database.py         # aiosqlite helpers called by data-api
│   └── requirements.txt
├── tests/
│   ├── test_db.py
│   ├── test_nodes.py
│   └── test_pipeline.py
└── data/                   # gitignored — SQLite file lives here (inside data-api container)
```

---

## Task Types

Handled by `agent/classifier.py`. Each incoming message is classified into one of:

| Type | Description | Downstream action |
|------|-------------|-------------------|
| `idea` | Startup idea or product concept | Nightly discovery pipeline |
| `shopping` | Request to find/buy a product | Immediate buyer agent (asks for deadline first) |
| `architecture` | Technical design decision | Saved to GitHub knowledge repo |
| `learning` | Lesson learned or personal insight | Saved to GitHub knowledge repo |
| `todo` | Actionable task | Saved only |
| `note` | Reference info, link, fact | Saved only (also the default before classification) |
| `question` | Open question to research | Saved only |

---

## Environment Variables

All vars are defined in `config.py` via `pydantic-settings`. See `.env.example`.

| Variable | Purpose |
|----------|---------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_USER_ID` | Your Telegram numeric user ID |
| `LLM_PROVIDER` | `gemini` (default) \| `claude` \| `openai` |
| `GOOGLE_GEMINI_API_KEY` | Required if `LLM_PROVIDER=gemini` |
| `ANTHROPIC_API_KEY` | Required if `LLM_PROVIDER=claude` |
| `OPENAI_API_KEY` | Required if `LLM_PROVIDER=openai` |
| `REDDIT_CLIENT_ID` | PRAW OAuth |
| `REDDIT_CLIENT_SECRET` | PRAW OAuth |
| `PRODUCT_HUNT_TOKEN` | PH GraphQL API |
| `GITHUB_TOKEN` | GitHub personal access token (for knowledge repo) |
| `GITHUB_REPO` | Target repo in `owner/repo` format |
| `HOME_LOCATION` | Fallback home location (e.g. `Moscow, Russia`) |
| `DATA_API_URL` | URL of data-api service (default: `http://data-api:8001`) |
| `DATA_API_KEY` | Shared secret for data-api auth (`X-API-Key` header) |
| `DISCOVERY_HOUR` | UTC hour for nightly run (default: 2) |
| `DISCOVERY_MINUTE` | UTC minute for nightly run (default: 0) |

---

## LLM Models Used

| Provider | Model |
|----------|-------|
| `gemini` | `gemini-3.1-flash-lite-preview` (via `langchain-google-genai`) |
| `claude` | `claude-sonnet-4-6` (via `langchain-anthropic`) |
| `openai` | `gpt-4o` (via `langchain-openai`) |

---

## Data Models

```python
# db/models.py
@dataclass
class Task:
    id: int
    text: str
    type: str        # idea | todo | note | learning | architecture | question | shopping
    created_at: datetime
    status: str      # pending | processing | done | error

@dataclass
class Source:
    platform: str    # reddit | hackernews | producthunt | indiehackers
    title: str
    url: str
    snippet: str

@dataclass
class DiscoveryResult:
    verdict: str
    score: float         # 0.0–10.0
    market_size: str
    competitors: list[str]
    sentiment_summary: str

@dataclass
class Offer:
    title: str
    url: str
    store: str
    price: str | None
    snippet: str | None
    delivery_days: int | None

@dataclass
class Discovery:
    id: int
    task_id: int
    ran_at: datetime
    reddit_summary: str | None
    hn_summary: str | None
    ph_summary: str | None
    ih_summary: str | None
    verdict: str | None
    score: float | None
    market_size: str | None
    full_report: dict | None   # {competitors, sentiment_summary, sources}
```

---

## LangGraph States

```python
# agent/state.py — Discovery pipeline
class DiscoveryState(TypedDict):
    idea_text: str
    reddit_sources: list[Source]
    hn_sources: list[Source]
    ph_sources: list[Source]
    ih_sources: list[Source]
    discovery: DiscoveryResult | None

# agent/buyer_graph.py — Buyer pipeline
class BuyerState(TypedDict):
    task_text: str
    search_query: str
    strategy: str           # asap | fast | week | flexible | any
    deadline_days: int | None
    current_location: str
    home_location: str
    offers: list[Offer]
```

---

## Telegram Commands

| Command | Description |
|---------|-------------|
| Any non-command text | Saved immediately, classified in background |
| `/list` | Last 10 tasks with ID, type emoji, status emoji, truncated text |
| `/report <id>` | Full discovery report for idea tasks |
| `/status` | Task counts (pending/done/error) + next scheduled run time |
| `/debug_run` | Manually triggers the nightly discovery job immediately |
| `/location` | Shows current home and current location |
| `/setlocation <city, country>` | Updates current location (used by buyer agent) |
| `/sethome <city, country>` | Updates home location (persisted in settings) |

---

## Shopping Flow (Stateful)

1. User sends a shopping message → saved as `note`, classified as `shopping`
2. Bot asks: *"When do you need this by?"*
3. Bot stores `awaiting_task_id`, `awaiting_search_query`, `awaiting_location_type` in settings
4. User replies with deadline text (e.g. "today", "end of week", "no rush")
5. `agent/deadline.py` parses it → `DeadlineInfo` with strategy
6. `bot/jobs/buyer.py:run_buyer()` invokes `buyer_graph` → DuckDuckGo search → ranked `Offer` list
7. Offers saved to DB; top 5 sent to Telegram with store links

---

## GitHub Knowledge Repo

`architecture` and `learning` tasks are saved as markdown files via `bot/integrations/github.py`:

- `knowledge/architecture/YYYY-MM-DD-<slug>.md`
- `knowledge/learnings/YYYY-MM-DD-<slug>.md`

Requires `GITHUB_TOKEN` and `GITHUB_REPO` to be set. Silently skipped if not configured.

---

## data-api Service

A FastAPI app (`data-api/main.py`) that owns the SQLite database. Key endpoints:

- `POST /tasks` — create task
- `GET /tasks` — list tasks (filterable by `status`, `type`, `limit`)
- `GET /tasks/{id}` — get task + discovery
- `PATCH /tasks/{id}/status` — update status
- `PATCH /tasks/{id}/type` — update type
- `PATCH /tasks/{id}/deadline` — set deadline + urgency
- `POST /tasks/{id}/discovery` — save discovery result
- `GET /tasks/{id}/discovery` — get discovery result
- `POST /tasks/{id}/offers` — save offer
- `GET /tasks/{id}/offers` — get offers
- `GET /settings/{key}` / `PUT /settings/{key}` — key-value settings store
- `GET /counts` — task counts by status
- `GET /health` — health check

All endpoints require `X-API-Key` header matching `DATA_API_KEY`.

---

## Running Locally

```bash
cp .env.example .env
# fill in .env

python -m venv .venv && source .venv/bin/activate
pip install -e .

python main.py
```

## Running with Docker

```bash
docker compose up -d
docker compose logs -f
```

The `idea-bot` container waits for `data-api` to be healthy before starting.

---

## Testing

```bash
pytest tests/                          # all tests
pytest tests/test_nodes.py -v          # node unit tests (mocked HTTP)
pytest tests/test_pipeline.py -v       # full pipeline (mocked LLM + nodes)
```

- Node tests mock all HTTP with `respx` or `unittest.mock`
- Pipeline tests use a real in-memory SQLite DB and mocked LLM responses
- No external API calls in tests

---

## Code Conventions

- All async — never use `requests` or `time.sleep`; use `httpx.AsyncClient` and `asyncio`
- No global state outside `config.py`
- `config.py` is imported as `from config import settings` — instantiated once
- All DB access via `db/client.py` HTTP calls — never import `db/database.py` from the bot
- Each research node must be independently runnable: `asyncio.run(reddit_node(state, config))`
- LangGraph graphs are compiled once at import time and reused across runs
- Secrets never logged — mask tokens in any debug output

---

## Adding a New Research Source

1. Create `agent/nodes/<source>.py` with `async def <source>_node(state: DiscoveryState) -> dict`
2. Return `{"<source>_sources": list[Source]}`
3. Add a parallel edge in `agent/graph.py`
4. Add `<source>_summary: str | None` to the discoveries table (migration in `data-api/database.py`)
5. Update the synthesize prompt in `agent/nodes/synthesize.py`
6. Add tests in `tests/test_nodes.py`

---

## Deployment Notes

- Docker Compose is the standard deployment unit — both services in the same compose file
- `fly.toml` present for Fly.io deployment
- SQLite is stored in a named Docker volume (`task_data`) — persists across container restarts
- Bot uses long polling — suitable for VPS; `post_init` handles 409 Conflict on restart
- `TELEGRAM_USER_ID` guard prevents the bot from responding to anyone else if the token leaks
