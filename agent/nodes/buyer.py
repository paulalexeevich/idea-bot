"""Buyer agent node — location-aware product search via DuckDuckGo."""
import asyncio
import logging
import re
from urllib.parse import urlparse

from db.models import Offer

logger = logging.getLogger(__name__)


def _extract_price(text: str) -> str | None:
    patterns = [
        r'[\$€£₽¥]\s?\d[\d\s,\.]*',
        r'\d[\d\s,\.]*\s?[\$€£₽¥]',
        r'\d[\d\s,\.]*\s?(?:руб|rub|usd|eur|USD|EUR|RUB)',
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


def _build_query(search_query: str, location_type: str, current_location: str, home_location: str) -> str:
    """Build a location-aware DuckDuckGo search query."""
    if location_type == "local":
        # Use current location (could be travel destination or home)
        place = current_location or home_location or ""
        if place:
            return f"{search_query} buy near {place}"
        return f"{search_query} buy near me"
    elif location_type == "online":
        return f"{search_query} buy online"
    else:
        # "any" — try both local and online results
        place = current_location or home_location or ""
        return f"{search_query} buy {place}".strip()


def _search_sync(query: str, max_results: int = 10) -> list[Offer]:
    from ddgs import DDGS
    offers: list[Offer] = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                offers.append(Offer(
                    title=r["title"],
                    price=_extract_price(r.get("body", "")),
                    store=_store_name(r["href"]),
                    url=r["href"],
                    snippet=r.get("body", "")[:200],
                ))
    except Exception as e:
        logger.warning("DuckDuckGo search failed: %s", e)
    return offers


async def buyer_node(state: dict) -> dict:
    search_query: str = state.get("search_query") or state["task_text"]
    location_type: str = state.get("location_type", "any")
    current_location: str = state.get("current_location", "")
    home_location: str = state.get("home_location", "")

    query = _build_query(search_query, location_type, current_location, home_location)
    logger.info("Buyer search [%s]: %s", location_type, query)

    offers = await asyncio.to_thread(_search_sync, query)
    return {"offers": offers}
