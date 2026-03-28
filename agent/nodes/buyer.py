"""Buyer agent — location-aware, deadline-aware product search with delivery estimation."""
import asyncio
import logging
import re
from urllib.parse import urlparse

from db.models import Offer

logger = logging.getLogger(__name__)

# Domains / patterns we know deliver fast to Hungary
_LOCAL_PATTERNS = ["budapest", ".hu", "emag.hu", "alza.hu", "mediamarkt.hu"]
_EU_PATTERNS = [".de", ".at", ".cz", ".sk", ".pl", ".ro", "amazon.de", "zalando", "zara", "uniqlo"]


def _estimate_delivery_days(url: str, snippet: str, location_type: str, current_location: str) -> int:
    """Rough delivery estimate in days based on store origin."""
    combined = (url + " " + snippet).lower()
    city = current_location.lower()

    if any(p in combined for p in [city, "pickup", "in store", "click & collect", "в магазин"]):
        return 0
    if any(p in combined for p in _LOCAL_PATTERNS):
        return 2
    if any(p in combined for p in _EU_PATTERNS):
        return 5
    return 10


def _extract_price(text: str) -> str | None:
    patterns = [
        r'[\$€£₽¥Ft]\s?\d[\d\s,\.]*',
        r'\d[\d\s,\.]*\s?[\$€£₽¥Ft]',
        r'\d[\d\s,\.]*\s?(?:HUF|Ft|EUR|USD|руб|RUB)',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(0).strip()
    return None


def _store_name(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return url


def _build_queries(search_query: str, strategy: str, current_location: str, home_location: str) -> list[str]:
    """Return 1-2 search queries based on deadline strategy."""
    place = current_location or home_location or ""

    if strategy == "asap":
        return [f"{search_query} buy store {place}",
                f"{search_query} available today {place}"]
    elif strategy == "fast":
        return [f"{search_query} buy {place}",
                f"{search_query} online fast delivery Hungary"]
    elif strategy == "week":
        return [f"{search_query} buy {place}",
                f"{search_query} online shop Europe delivery"]
    else:
        # flexible / any — wide global search
        return [f"{search_query} buy online best price",
                f"{search_query} shop compare price"]


def _search_sync(query: str, max_results: int = 8) -> list[dict]:
    from ddgs import DDGS
    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append(r)
    except Exception as e:
        logger.warning("DuckDuckGo search failed for '%s': %s", query, e)
    return results


async def buyer_node(state: dict) -> dict:
    search_query: str = state.get("search_query") or state["task_text"]
    strategy: str = state.get("strategy", "any")
    current_location: str = state.get("current_location", "")
    home_location: str = state.get("home_location", "")
    deadline_days: int | None = state.get("deadline_days")

    queries = _build_queries(search_query, strategy, current_location, home_location)

    # Run queries concurrently
    raw_results = await asyncio.gather(*[
        asyncio.to_thread(_search_sync, q) for q in queries
    ])

    # Deduplicate by URL
    seen_urls: set[str] = set()
    offers: list[Offer] = []

    for results in raw_results:
        for r in results:
            url = r.get("href", "")
            if url in seen_urls:
                continue
            seen_urls.add(url)

            delivery_days = _estimate_delivery_days(url, r.get("body", ""), strategy, current_location)

            # Skip if deadline is tighter than estimated delivery
            if deadline_days is not None and delivery_days > deadline_days:
                continue

            offers.append(Offer(
                title=r["title"],
                price=_extract_price(r.get("body", "")),
                store=_store_name(url),
                url=url,
                snippet=r.get("body", "")[:200],
                delivery_days=delivery_days,
            ))

    # Sort: delivery_days ASC, then price presence (priced first)
    offers.sort(key=lambda o: (o.delivery_days if o.delivery_days is not None else 99,
                               0 if o.price else 1))

    logger.info("Buyer [%s, deadline=%s days]: %d offers after filtering", strategy, deadline_days, len(offers))
    return {"offers": offers}
