"""Tests for the LangGraph discovery pipeline state flow."""
import pytest
from unittest.mock import AsyncMock, patch

from db.models import DiscoveryResult, Source


MOCK_SOURCES = [
    Source(platform="hackernews", title="Recipe AI startup", url="https://hn.com/1", snippet="People love it"),
    Source(platform="reddit", title="r/startups thread", url="https://reddit.com/r/1", snippet="High demand"),
]

MOCK_RESULT = DiscoveryResult(
    verdict="Strong demand signal. Dietary restriction tracking is an underserved niche.",
    score=7.5,
    market_size="~$2B TAM in food-tech, SAM ~$200M for dietary-specific tools",
    competitors=["Yummly", "Whisk", "Mealime"],
    sentiment_summary="Users frequently complain about lack of personalized dietary options.",
)


@pytest.mark.asyncio
async def test_discovery_graph_state_flow():
    """Pipeline passes idea_text through all nodes and collects discovery result."""
    with patch("agent.nodes.reddit.reddit_node", new=AsyncMock(return_value={"reddit_sources": MOCK_SOURCES[:1]})), \
         patch("agent.nodes.hackernews.hackernews_node", new=AsyncMock(return_value={"hn_sources": MOCK_SOURCES[1:]})), \
         patch("agent.nodes.producthunt.producthunt_node", new=AsyncMock(return_value={"ph_sources": []})), \
         patch("agent.nodes.indiehackers.indiehackers_node", new=AsyncMock(return_value={"ih_sources": []})), \
         patch("agent.nodes.synthesize.synthesize_node", new=AsyncMock(return_value={"discovery": MOCK_RESULT})):

        import importlib
        import agent.graph
        importlib.reload(agent.graph)
        from agent.graph import discovery_graph

        state = await discovery_graph.ainvoke({
            "idea_text": "AI recipe generator for dietary restrictions",
            "reddit_sources": [], "hn_sources": [],
            "ph_sources": [], "ih_sources": [],
            "discovery": None,
        })

    assert state["discovery"] is not None
    assert state["discovery"].score == 7.5
    assert state["discovery"].verdict.startswith("Strong demand")


@pytest.mark.asyncio
async def test_discovery_graph_collects_all_sources():
    """Sources from all 4 nodes end up in the final state."""
    hn = Source(platform="hackernews", title="HN post", url="https://hn.com/2", snippet="s")
    rd = Source(platform="reddit", title="Reddit post", url="https://reddit.com/2", snippet="s")
    ph = Source(platform="producthunt", title="PH product", url="https://ph.com/2", snippet="s")
    ih = Source(platform="indiehackers", title="IH post", url="https://ih.com/2", snippet="s")

    with patch("agent.nodes.reddit.reddit_node", new=AsyncMock(return_value={"reddit_sources": [rd]})), \
         patch("agent.nodes.hackernews.hackernews_node", new=AsyncMock(return_value={"hn_sources": [hn]})), \
         patch("agent.nodes.producthunt.producthunt_node", new=AsyncMock(return_value={"ph_sources": [ph]})), \
         patch("agent.nodes.indiehackers.indiehackers_node", new=AsyncMock(return_value={"ih_sources": [ih]})), \
         patch("agent.nodes.synthesize.synthesize_node", new=AsyncMock(return_value={"discovery": MOCK_RESULT})):

        import importlib
        import agent.graph
        importlib.reload(agent.graph)
        from agent.graph import discovery_graph

        state = await discovery_graph.ainvoke({
            "idea_text": "test idea",
            "reddit_sources": [], "hn_sources": [],
            "ph_sources": [], "ih_sources": [],
            "discovery": None,
        })

    assert len(state["reddit_sources"]) == 1
    assert len(state["hn_sources"]) == 1
    assert len(state["ph_sources"]) == 1
    assert len(state["ih_sources"]) == 1
