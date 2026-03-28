from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from agent.nodes.buyer import buyer_node
from db.models import Offer


class BuyerState(TypedDict):
    task_text: str
    search_query: str
    strategy: str           # asap | fast | week | flexible | any
    deadline_days: int | None
    current_location: str
    home_location: str
    offers: list[Offer]


def build_buyer_graph():
    graph = StateGraph(BuyerState)
    graph.add_node("buyer", buyer_node)
    graph.add_edge(START, "buyer")
    graph.add_edge("buyer", END)
    return graph.compile()


buyer_graph = build_buyer_graph()
