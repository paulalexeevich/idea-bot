import json

from config import settings
from db.models import DiscoveryResult, Source


def _build_prompt(idea_text: str, all_sources: list[Source]) -> str:
    sources_text = "\n".join(
        f"[{s.platform.upper()}] {s.title}\n  URL: {s.url}\n  Snippet: {s.snippet}"
        for s in all_sources
    )
    return f"""You are a startup idea validator. Analyze the following idea and the research sources found about it.

IDEA:
{idea_text}

RESEARCH SOURCES ({len(all_sources)} found):
{sources_text if sources_text else "No sources found."}

Respond with a JSON object with exactly these fields:
{{
  "verdict": "<2-4 sentence assessment of whether this idea is worth building as a startup>",
  "score": <float 0.0-10.0, where 10 = extremely promising>,
  "market_size": "<rough TAM/SAM estimate with reasoning>",
  "competitors": ["<competitor name or product>", ...],
  "sentiment_summary": "<summary of community sentiment and pain points from the sources>"
}}

Be honest and critical. A high score means strong evidence of demand, clear market, and differentiation opportunity."""


async def synthesize_node(state) -> dict:
    idea_text: str = state["idea_text"]
    all_sources: list[Source] = (
        state.get("reddit_sources", [])
        + state.get("hn_sources", [])
        + state.get("ph_sources", [])
        + state.get("ih_sources", [])
    )

    prompt = _build_prompt(idea_text, all_sources)

    if settings.llm_provider == "claude":
        result = await _call_claude(prompt)
    elif settings.llm_provider == "openai":
        result = await _call_openai(prompt)
    elif settings.llm_provider == "gemini":
        result = await _call_gemini(prompt)
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider}")

    return {"discovery": result}


async def _call_claude(prompt: str) -> DiscoveryResult:
    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import HumanMessage

    llm = ChatAnthropic(model="claude-sonnet-4-6", api_key=settings.anthropic_api_key)
    response = await llm.ainvoke([HumanMessage(content=prompt)])
    return _parse_response(response.content)


async def _call_openai(prompt: str) -> DiscoveryResult:
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage

    llm = ChatOpenAI(model="gpt-4o", api_key=settings.openai_api_key)
    response = await llm.ainvoke([HumanMessage(content=prompt)])
    return _parse_response(response.content)


async def _call_gemini(prompt: str) -> DiscoveryResult:
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langchain_core.messages import HumanMessage

    llm = ChatGoogleGenerativeAI(model="gemini-2.5-pro", google_api_key=settings.google_gemini_api_key)
    response = await llm.ainvoke([HumanMessage(content=prompt)])
    return _parse_response(response.content)


def _parse_response(content) -> DiscoveryResult:
    # Gemini returns a list of dicts: [{'type': 'text', 'text': '...'}]
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else (part.text if hasattr(part, "text") else str(part))
            for part in content
        )
    text = content.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    data = json.loads(text.strip())
    return DiscoveryResult(
        verdict=data["verdict"],
        score=float(data["score"]),
        market_size=data["market_size"],
        competitors=data.get("competitors", []),
        sentiment_summary=data["sentiment_summary"],
    )
