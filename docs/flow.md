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

    B --> C{AWAITING state\nin settings?}
    C -->|awaiting_reminder_task_id| D[_handle_reminder_date_reply\nor _handle_reminder_time_reply\n→ save_message + process-now]
    C -->|awaiting_task_id| E[_handle_deadline_reply\n→ run_buyer\n→ save_message + process-now]
    C -->|nothing| F

    F[save_message user + text\nset last_user_message_at] --> G
    G[create_task type=note\n→ reply 'Task #N saved ✓'\nsave_message bot + reply] --> H

    H[asyncio.create_task\n_classify_and_followup] -->|background| I

    subgraph CLASSIFY [Classification — runs in background]
        I[get_recent_messages 20\nshort-term context] --> J
        J{MEMORY_AGENT_URL set?}
        J -->|yes| K[MCP query_memory text\n→ Neo4j keyword search\n→ formatted context string]
        J -->|no| L[no long-term context]
        K --> M
        L --> M
        M[classify_task\nwith_structured_output\ngemini-3.1-flash-lite\ninjects both contexts] --> N[set_task_type]
    end

    N --> R{type?}

    R -->|idea| S[queue: nightly discovery\nreply with time]
    R -->|shopping| T[ask deadline\nset AWAITING_TASK_KEY]
    R -->|reminder| U{due_date\n+due_time\nextracted?}
    U -->|both| V[update_task_reminder\nreply confirmed]
    U -->|partial| W[set AWAITING_REMINDER_TASK_KEY\nask for missing piece]
    U -->|neither| W
    R -->|architecture\nlearning| X[save_to_github\nreply with link]
    R -->|todo note\nquestion other| Y[emoji reply]

    D --> Z
    E --> Z
    S --> Z
    T --> Z
    V --> Z
    W --> Z
    X --> Z
    Y --> Z

    Z[save_message bot + reply\nPOST /memory/process-now\n→ Tier 1 extraction]
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
        CL[classifier.py] -->|MCP| MA
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
