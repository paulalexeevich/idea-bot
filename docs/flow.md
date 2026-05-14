# System Flow Diagrams

## 1. Service map

```mermaid
graph TB
    TG[Telegram] -->|long polling| BOT[idea-bot<br/>PTB]
    BOT -->|HTTP + X-API-Key| API[data-api<br/>FastAPI :8001]
    BOT -->|HTTP POST /memory/*| MEM[memory-agent<br/>FastAPI :8002]
    BOT -->|MCP streamable_http /mcp| MEM
    API -->|aiosqlite| DB[(SQLite<br/>tasks.db)]
    MEM -->|bolt| NEO[(Neo4j<br/>:7687)]
    MEM -->|HTTP GET /messages/*| API
    DASH[dashboard<br/>Next.js :3000] -->|HTTP| API
    USER((User)) -->|message| TG
    USER -->|browser| DASH
```

---

## 2. Every incoming message — full path

```mermaid
flowchart TD
    A([User sends text]) --> B[handle_message]
    B --> C[save_message user async\nset last_user_message_at async]
    C --> D{AWAITING state?}
    D -->|awaiting_reminder_*| E[_handle_reminder_date_reply\nor _handle_reminder_time_reply\n→ save + Tier 1]
    D -->|awaiting_task_id| F[_handle_deadline_reply\n→ run_buyer + save + Tier 1]
    D -->|none| G[send typing action\nget_recent_messages 20]
    G --> H[run_unified_agent\ntext + recent + user_tz]

    subgraph AGENT [Unified agent — up to 10 rounds]
        H --> I{LLM decides\nnext tool}
        I -->|query_memory| J[Neo4j search\nvia MCP]
        I -->|list_tasks| K[GET /tasks\nfiltered]
        I -->|search_tasks| L[GET /tasks/search\nkeyword]
        I -->|save_reminder| M[POST /tasks type=reminder\nPATCH /reminder\nset AWAITING if partial]
        I -->|save_task| N[POST /tasks\nany type]
        I -->|ask_clarification| O[return question\nas reply]
        J & K & L --> I
        M & N & O --> P[LLM writes\nfinal reply]
    end

    P --> Q[AgentResult\nreply + task_id + task_type + awaiting]
    Q --> R[reply_text to Telegram\nsave_message bot + Tier 1\nset awaiting settings\nGitHub if arch/learning]
```

---

## 3. Memory system — 3 tiers

```mermaid
flowchart TD
    subgraph T1 [Tier 1 — Per exchange, immediate]
        A1[POST /memory/process-now\ntriggered after every bot reply] --> B1
        B1[GET /messages/unprocessed\nfrom data-api] --> C1
        C1[extract_graph messages\nLLM: gemini-2.5-flash\nextract explicit facts only] --> D1
        D1[Neo4j merge_nodes_and_edges] --> E1
        E1[POST /messages/processed\nmark processed=1]
    end

    subgraph T2 [Tier 2 — Session idle, 10 min quiet]
        A2[check_session_idle job\nevery 60s] --> B2
        B2{last_user_message_at\n> 10 min ago\nAND not yet extracted?}
        B2 -->|yes| C2[POST /memory/process-session]
        B2 -->|no| SKIP2[skip]
        C2 --> D2[GET /messages/recent 30\nfrom data-api] --> E2
        E2[extract_session messages\nLLM: gemini-2.5-flash\nfind patterns across session] --> F2
        F2[Neo4j merge_nodes_and_edges\nset session_extracted_at]
    end

    subgraph T3 [Tier 3 — Daily reflection, 03:00 UTC]
        A3[daily_reflection job\n03:00 UTC] --> B3
        B3[POST /memory/reflect] --> C3
        C3[get_graph_summary\n+ GET /messages/recent 100] --> D3
        D3[reflect_on_graph\nLLM: gemini-2.5-flash\nmerge duplicates\nadd patterns\nprune stale] --> E3
        E3[Apply merge plan:\nmerge_duplicate_nodes\nadd nodes+edges\ndelete_nodes]
    end

    subgraph NEO [Neo4j knowledge graph]
        N1[Person]
        N2[Preference]
        N3[RecurringEvent]
        N4[Place]
        N5[Topic]
    end

    E1 --> NEO
    F2 --> NEO
    E3 --> NEO

    NEO -->|query_memory MCP tool\nat classification time| INJECT[injected into\nclassifier prompt\nas long_term_context]
```

---

## 4. MCP tool call during classification

```mermaid
sequenceDiagram
    participant H as handle_message
    participant C as classifier.py
    participant MA as memory-agent /mcp
    participant NEO as Neo4j

    H->>C: classify_task(text, recent_msgs)
    C->>MA: MCP query_memory(text)
    MA->>NEO: MATCH nodes WHERE name CONTAINS terms
    NEO-->>MA: matching nodes + relationships
    MA-->>C: formatted context string
    Note over C: "- Person 'Olga': relation=wife<br/>  → KNOWS User"
    C->>C: build system prompt with both contexts
    C->>C: LLM with_structured_output
    C-->>H: TaskClassification(type, title,<br/>due_date, due_time, ...)
```

---

## 5. Shopping flow — two-step with deadline

```mermaid
flowchart TD
    A[classified: shopping] --> B
    B[set AWAITING_TASK_KEY\nset AWAITING_SEARCH_QUERY\nset AWAITING_LOCATION_TYPE] --> C
    C[ask: 'When do you need this by?']

    D([Next user message]) --> E[_handle_deadline_reply]
    E --> F[clear AWAITING_TASK_KEY\nparse_deadline via LLM\ngemini-3.1-flash-lite] --> G
    G[update_task_deadline\nstrategy: asap/fast/week/flexible] --> H

    H[run_buyer] --> I

    subgraph BUYER [Buyer pipeline — LangGraph]
        I[buyer_graph.ainvoke] --> J
        J[buyer_node] --> K
        K[_build_queries\nbased on strategy + location] --> L
        L[DuckDuckGo search\nasync parallel queries] --> M
        M[_estimate_delivery_days\nfilter by deadline] --> N
        N[sort by delivery_days then price]
    end

    N --> O[save_offer × N]
    O --> P[Telegram: top 5 with prices and links]
    P --> Q[save_message bot + reply\nPOST /memory/process-now\n→ Tier 1 extraction]
```

---

## 6. Reminder flow — two-step date+time

```mermaid
flowchart TD
    A[classified: reminder] --> B{due_date\ndue_time\nextracted?}

    B -->|both present\n'tomorrow at 10'| SAVE[update_task_reminder\ndue_date + due_time\nreply confirmed\n— no extra question]

    B -->|only date| C[set AWAITING_REMINDER_TASK_KEY\nset AWAITING_REMINDER_DATE=date\nset AWAITING_REMINDER_TIME=NEEDED\nask for time]

    B -->|only time| D[set AWAITING_REMINDER_TASK_KEY\nset AWAITING_REMINDER_TIME=time\nset AWAITING_REMINDER_DATE=NEEDED\nask for date]

    B -->|neither| E[set AWAITING states\nask for date and time]

    F([Next message]) --> G{which piece\nstill NEEDED?}
    G -->|date| H[_handle_reminder_date_reply\nparse_reminder_datetime LLM\n(extracts date+time together)]
    G -->|time| I[_handle_reminder_time_reply\nparse_reminder_datetime LLM first\nfallback: time_parser regex]

    H --> SAVE
    I --> SAVE

    SAVE --> SAVEMSG[save_message bot + reply\nPOST /memory/process-now\n→ Tier 1 extraction]
    SAVEMSG --> FIRE

    subgraph FIRE [check_reminders job — every 60s]
        J[GET /reminders/due?now=HH:MM\ndue_date+due_time <= now\nnotified_at IS NULL] --> K
        K[send Telegram message] --> L
        L[POST /tasks/id/notified\nsets notified_at + status=done]
    end
```

---

## 7. Idea discovery pipeline — nightly

```mermaid
flowchart TD
    A[run_discovery job\n02:00 UTC daily] --> B
    B[get_pending_tasks type=idea] --> C

    subgraph LANGGRAPH [LangGraph — discovery_graph]
        C --> D[set status=processing]
        D --> E

        E1[reddit_node\nPRAW] & E2[hackernews_node\nAlgolia API] & E3[producthunt_node\nPH GraphQL] & E4[indiehackers_node\nhttpx + BS4] --> E[parallel fan-out]

        E --> F[synthesize_node\ngemini-2.5-pro\nverdicts + score + competitors]
    end

    F --> G[save_discovery]
    G --> H[set_task_status done]
    H --> I[Telegram: 'Discovery complete\nScore X/10 — use /report N']
```

---

## 8. Scheduled jobs timeline

```
UTC time    Job                     Trigger         What it does
──────────────────────────────────────────────────────────────────────────────
Every 10s   (startup warmup)
Every 60s   check_reminders         run_repeating   Fire due date+time reminders
Every 60s   check_completions       run_repeating   Notify newly-done tasks
Every 60s   check_session_idle      run_repeating   Tier 2 if user quiet 10+ min
02:00 UTC   run_discovery           run_daily       Nightly idea validation
03:00 UTC   daily_reflection        run_daily       Tier 3 Neo4j graph cleanup

Event-driven (not scheduled):
  After every bot reply → POST /memory/process-now  (Tier 1, immediate)
  After shopping classified → ask deadline → run_buyer (immediate)
  After reminder classified → ask date/time if missing (interactive)
```

---

## 9. Data flow between services

```mermaid
flowchart LR
    subgraph BOT [idea-bot process]
        HM[handle_message] --> DC[db/client.py]
        UA[unified_agent.py] -->|MCP| MA
        TJ[jobs/memory.py] -->|HTTP| MA
        RJ[jobs/reminders.py] --> DC
        NJ[jobs/notifier.py] --> DC
        DJ[jobs/discovery.py] --> DC
        BJ[jobs/buyer.py] --> DC
    end

    subgraph API [data-api process]
        DC -->|HTTP REST| EP[endpoints]
        EP --> SQ[(SQLite)]
    end

    subgraph MEM [memory-agent process]
        MA[/mcp + /memory/*] --> EX[extractor.py\nLLM calls]
        MA --> GC[graph_client.py]
        MA -->|GET /messages/*| EP
        GC --> NEO[(Neo4j)]
    end
```

---

## 10. Query agent flow (superseded)

> **This flow is no longer active.** Query handling is now done inside the unified agent loop (see Diagram 2). `agent/query_agent.py` and `_handle_query` are kept for reference but are not called from `bot/handlers/idea.py`.
>
> In the new flow, when the LLM determines a message is a query it calls `list_tasks` and/or `search_tasks` within the unified agent loop, then calls `save_task(type="query")` to record it, and returns a formatted reply — all in one run.

```mermaid
flowchart TD
    A([User asks a question]) --> B[_QUERY_RE matches\nclassify inline]
    B -->|type=query| C[_handle_query]
    C --> D[query_agent.run_query\nrecent_messages + user_tz]

    subgraph AGENT [Query agent loop — up to 6 rounds]
        D --> E{LLM decides\nwhich tool}
        E -->|list_tasks| F[GET /tasks\nfiltered by type+status]
        E -->|search_tasks| G[GET /tasks/search?q=\nLIKE keyword match]
        E -->|query_memory| H[Neo4j keyword search\nvia MCP]
        F & G & H --> I[ToolMessage result\nback to LLM]
        I --> E
        E -->|no more tools| J[LLM formats reply]
    end

    J --> K[reply_text to user]
    K --> L[save_message bot\nPOST /memory/process-now\nTier 1 extraction]
```
