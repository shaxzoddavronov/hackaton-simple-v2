from __future__ import annotations

from langgraph.graph import END, StateGraph

from app.agents.nodes import (
    answer_writer,
    chart_designer,
    coordinator,
    error_responder,
    finalizer,
    query_executor,
    query_planner,
    query_validator,
    rag_retriever,
    schema_loader,
)
from app.agents.state import GraphState

MAX_PLANNER_ATTEMPTS = 2
MAX_EXECUTOR_ATTEMPTS = 2


def _route_after_coordinator(state: GraphState) -> str:
    intent = state.get("intent", "clarify")
    if intent == "chitchat":
        return "answer_writer"
    if intent == "clarify":
        return "finalizer"
    if intent == "metadata":
        return "schema_loader"
    if intent in {"data_query", "dashboard"}:
        return "schema_loader"
    return "finalizer"


def _route_after_schema(state: GraphState) -> str:
    if state.get("error_message"):
        return "error_responder"
    intent = state.get("intent", "")
    if intent == "metadata":
        return "answer_writer"
    # data_query / dashboard → go through RAG before planning so the planner
    # gets semantically pruned tables + retrieved API/doc context.
    return "rag_retriever"


def _route_after_validation(state: GraphState) -> str:
    val = state.get("validation")
    if val is not None and val.ok:
        return "query_executor"
    if int(state.get("planner_attempts", 0)) >= MAX_PLANNER_ATTEMPTS:
        return "error_responder"
    return "query_planner"


def _route_after_executor(state: GraphState):
    if state.get("result") is not None:
        # Parallel fan-out: chart + answer both run, finalizer merges.
        return ["chart_designer", "answer_writer"]
    if int(state.get("executor_attempts", 0)) >= MAX_EXECUTOR_ATTEMPTS:
        return "error_responder"
    return "query_planner"


def build_graph():
    g: StateGraph = StateGraph(GraphState)

    g.add_node("coordinator", coordinator.run)
    g.add_node("schema_loader", schema_loader.run)
    g.add_node("rag_retriever", rag_retriever.run)
    g.add_node("query_planner", query_planner.run)
    g.add_node("query_validator", query_validator.run)
    g.add_node("query_executor", query_executor.run)
    g.add_node("chart_designer", chart_designer.run)
    g.add_node("answer_writer", answer_writer.run)
    g.add_node("finalizer", finalizer.run)
    g.add_node("error_responder", error_responder.run)

    g.set_entry_point("coordinator")

    g.add_conditional_edges(
        "coordinator",
        _route_after_coordinator,
        {
            "answer_writer": "answer_writer",
            "schema_loader": "schema_loader",
            "finalizer": "finalizer",
        },
    )

    g.add_conditional_edges(
        "schema_loader",
        _route_after_schema,
        {
            "answer_writer": "answer_writer",
            "rag_retriever": "rag_retriever",
            "error_responder": "error_responder",
        },
    )

    g.add_edge("rag_retriever", "query_planner")
    g.add_edge("query_planner", "query_validator")

    g.add_conditional_edges(
        "query_validator",
        _route_after_validation,
        {
            "query_executor": "query_executor",
            "query_planner": "query_planner",
            "error_responder": "error_responder",
        },
    )

    g.add_conditional_edges(
        "query_executor",
        _route_after_executor,
        {
            "chart_designer": "chart_designer",
            "answer_writer": "answer_writer",
            "query_planner": "query_planner",
            "error_responder": "error_responder",
        },
    )

    g.add_edge("chart_designer", "finalizer")
    g.add_edge("answer_writer", "finalizer")
    g.add_edge("error_responder", "finalizer")
    g.add_edge("finalizer", END)

    return g.compile()


_compiled = None


def get_graph():
    global _compiled
    if _compiled is None:
        _compiled = build_graph()
    return _compiled


__all__ = ["build_graph", "get_graph", "MAX_PLANNER_ATTEMPTS", "MAX_EXECUTOR_ATTEMPTS"]
