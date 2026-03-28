from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Task:
    id: int
    text: str
    type: str  # idea | todo | note | ...
    created_at: datetime
    status: str  # pending | processing | done | error


@dataclass
class Source:
    platform: str  # reddit | hackernews | producthunt | indiehackers
    title: str
    url: str
    snippet: str


@dataclass
class DiscoveryResult:
    verdict: str
    score: float  # 0.0–10.0
    market_size: str
    competitors: list[str]
    sentiment_summary: str


@dataclass
class Offer:
    title: str
    url: str
    store: str
    price: str | None = None
    snippet: str | None = None
    delivery_days: int | None = None


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
    full_report: dict | None  # parsed JSON
