"""
One chat turn = the model picks a tool, the tool gathers the facts, then the
model answers from them.

Built as a small LangGraph graph:

    question ─► choose_tool ─┬─► retrieve (vector search) ──┬─► build_prompt ─► END
     (LLM tool call)         │        └─ (both tools) ──┐   │
                             └─► run_table (pandas) ◄───┘───┘

  * The model sees two tools and calls one (or both):
      search_laureate_articles  "What did Salam work on?" -> the two-layer retriever
      query_nobel_table         "How many...", "youngest" -> table_tool.py
    Vector search can't COUNT, so counting questions need the table.
  * The table tool's arguments ARE the TablePlan form, so choosing the tool
    and filling the form happen in one model call.
  * Safety nets: if the model calls no tool, the keyword regex picks one; if
    its table form is invalid, a second call fills it with a strict JSON schema.
  * Thinking is always off: with tools the answers were better and much faster.

The answer itself is streamed afterwards by stream_answer(), outside the
graph, so the app can show it word by word.

Try it in the terminal (Ollama must be running):
    D:\\python_evenv\\Scripts\\python.exe -m chatbot.chat "How many laureates were born in Spain?"
"""

import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from typing import Optional, TypedDict

from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, ValidationError

from chatbot.config import HISTORY_TURNS
from chatbot.llm import get_llm
from chatbot.retriever import LaureateRetriever, RetrievalResult, format_context
from chatbot.table_tool import TablePlan, fix_plan, run_plan
from chatbot.tracing import Trace

# ===========================================================================
# Prompts. Small models need short, direct rules.
# ===========================================================================
TEXT_PROMPT = """You are a helpful assistant that answers questions about Nobel Prize laureates.

Rules:
- Answer ONLY from the numbered sources below. Do not use outside knowledge.
- After each fact, cite its source number in square brackets, e.g. [2].
- If the sources do not contain the answer, say you could not find it in the laureate data. Do not guess.
- The "Nobel Prize:" line in a profile is the official prize category and year.
- You only see a few sources, not the whole list of laureates. If asked to count, rank or list ALL laureates matching something ("how many...", "the youngest..."), say you cannot give a complete answer, then mention only the matching examples found in the sources.
- Be concise: a short paragraph or a few bullet points.

Sources:
{context}"""

TABLE_PROMPT = """You are a helpful assistant that answers questions about Nobel Prize laureates.

Below are results of lookups in the official Nobel Prize data table (all laureates up to today).
Rules:
- Answer ONLY from these results. The counts and rankings are exact; repeat them, do not recalculate or recount the names.
- For youngest/oldest, give the ANSWER line of each result. The ranking list below it is only context.
- Say briefly what was counted (e.g. "laureates born in Spain, using today's borders").
- If a result has a Note, mention it in one sentence. Do not add any other notes or explanations of your own.
- Name the laureates when the list is short (up to about 10); otherwise give the count and a few examples.
- End with: (Source: Nobel Prize data table)

Table results:
{results}"""

# Both tools were used: exact table results plus article excerpts.
BOTH_PROMPT = """You are a helpful assistant that answers questions about Nobel Prize laureates.

Rules:
- Answer ONLY from the table results and numbered sources below. Do not use outside knowledge.
- Counts and rankings come from the table results: they are exact, repeat them, do not recount.
- For facts from the sources, cite the source number in square brackets, e.g. [2].
- If neither contains the answer, say you could not find it in the laureate data.
- Be concise.

Table results (official Nobel Prize data):
{results}

Sources:
{context}"""

TOOL_PROMPT = """You answer questions about Nobel Prize laureates. First call a tool to get the facts. Today is {today}.

Tools:
- query_nobel_table: the official table of ALL laureates. Use it for counting, ranking or complete lists:
  "how many", "youngest", "oldest", "list all", "which country has the most", percentages.
  Fill one query per separate thing asked (at most 3). Leave filters null unless the question asks for them.
- search_laureate_articles: Wikipedia articles about laureates. Use it for everything else:
  a laureate's life, work, discoveries, prizes, quotes, or who did something.
  Do not add names the user did not mention, except to replace he/she/they from the previous question.
Call both tools only if the question asks for a count AND for article facts.

Table examples:
"How many Nobel laureates were born in Spain?" -> [{{"action": "count", "birth_country": "Spain"}}]
"Who is the third youngest and the oldest laureate?" -> [{{"action": "youngest", "rank": 3}}, {{"action": "oldest", "rank": 1}}]
"How many laureates did their undergrad at Harvard?" -> [{{"action": "count", "university": "Harvard", "undergraduate_only": true}}]
"Which women won the Chemistry prize after 2000?" -> [{{"action": "list", "gender": "female", "category": "Chemistry", "year_from": 2001}}]
"Which country has the most laureates?" -> [{{"action": "count_by_country"}}]
"How many laureates were born in California?" -> [{{"action": "count", "birth_place": "California"}}]
"How many organisations won the Peace Prize?" -> [{{"action": "count", "category": "Peace", "organizations": true}}]"""

# Fallback only: used when the table tool's arguments are not a valid form.
PLANNER_PROMPT = """Turn the user's question about Nobel laureates into table lookups. Today is {today}.
Fill one query per separate thing asked (at most 3). Leave filters null unless the question asks for them.

Examples:
"How many Nobel laureates were born in Spain?" -> [{{"action": "count", "birth_country": "Spain"}}]
"Who is the third youngest and the oldest laureate?" -> [{{"action": "youngest", "rank": 3}}, {{"action": "oldest", "rank": 1}}]
"How many laureates did their undergrad at Harvard?" -> [{{"action": "count", "university": "Harvard", "undergraduate_only": true}}]
"Which women won the Chemistry prize after 2000?" -> [{{"action": "list", "gender": "female", "category": "Chemistry", "year_from": 2001}}]
"Which country has the most laureates?" -> [{{"action": "count_by_country"}}]
"How many laureates were born in California?" -> [{{"action": "count", "birth_place": "California"}}]
"How many organisations won the Peace Prize?" -> [{{"action": "count", "category": "Peace", "organizations": true}}]"""

# Words that point back to someone mentioned earlier ("What did HE do later?").
_PRONOUNS = re.compile(r"\b(he|she|him|his|her|hers|they|them|their|it|its|this|that)\b", re.I)

# Fallback when the model calls no tool: questions that need counting,
# ranking or a complete list -> the table.
_TABLE_QUESTION = re.compile(
    r"\b(how many|number of|count|youngest|oldest|list (?:all|every)|all (?:the )?(?:laureates|winners)"
    r"|which countr(?:y|ies)|most (?:laureates|winners|nobel|prizes)|percentage|proportion)\b",
    re.I,
)


# ===========================================================================
# The two tools, as the model sees them. The class name is the tool name,
# the docstring is the tool description and the fields are its arguments.
# ===========================================================================
class search_laureate_articles(BaseModel):
    """Search the Wikipedia articles about Nobel laureates. Use for a laureate's life, work, discoveries, prizes, or who did something."""
    query: str = Field(description="What to search for. Replace he/she/they with the laureate's name from the previous question.")


class query_nobel_table(TablePlan):
    """Look up the official Nobel Prize data table of all laureates. Use for counting, ranking (youngest/oldest), complete lists, or comparing countries."""



# ===========================================================================
# What one turn produces (used by app.py and eval_chat.py)
# ===========================================================================
@dataclass
class Turn:
    question: str
    route: str                                  # "text", "table" or "text+table" (both tools)
    messages: list                              # ready to send to the LLM
    search_query: str = ""                      # text route: may include the previous question
    retrieval: Optional[RetrievalResult] = None  # text route
    sources: list[Document] = field(default_factory=list)  # text route, numbered from 1
    plan: Optional[TablePlan] = None            # table route: the filled-in form(s)
    table_result: str = ""                      # table route: what pandas returned
    trace: Optional[Trace] = None               # step-by-step log; call trace.finish(answer)


class State(TypedDict, total=False):
    """The data that flows between the graph's steps."""
    trace: Trace
    question: str
    history: list[dict]
    route: str
    search: bool                # the article search tool was chosen
    model_query: str            # the search text the model wrote
    search_query: str
    retrieval: RetrievalResult
    sources: list[Document]
    context: str                # the numbered sources as text
    plan: TablePlan
    table_result: str
    messages: list


def _history_messages(history: list[dict]) -> list:
    """Only the last HISTORY_TURNS question/answer pairs: enough to follow the
    conversation, small enough to leave room for the sources."""
    return [(HumanMessage if m["role"] == "user" else AIMessage)(m["content"])
            for m in history[-2 * HISTORY_TURNS:]]


@lru_cache(maxsize=1)
def _tool_chooser():
    """The LLM with both tools attached. Thinking off: fast, and better at tool calls."""
    return get_llm(thinking=False).bind_tools([search_laureate_articles, query_nobel_table])


@lru_cache(maxsize=1)
def _planner():
    """Fallback: the LLM forced to reply with a TablePlan (JSON matching the schema)."""
    return get_llm(thinking=False).with_structured_output(TablePlan, method="json_schema")


def _ask_with_previous(state: State) -> str:
    """Adds the previous question, so "And how many in Italy?" or "When did
    she die?" can reuse what was asked before."""
    previous = [m["content"] for m in state["history"] if m["role"] == "user"]
    if previous:
        return f"Previous question: {previous[-1]}\nQuestion: {state['question']}"
    return state["question"]


def _fallback_plan(ask: str, trace: Trace) -> Optional[TablePlan]:
    """Fill the table form with a strict JSON schema (second model call)."""
    try:
        plan = _planner().invoke([SystemMessage(PLANNER_PROMPT.format(today=date.today())),
                                  HumanMessage(ask)])
    except Exception as err:  # the model returned something that isn't a valid form
        trace.error("plan.fallback", err)
        return None
    return plan if plan and plan.queries else None


def build_graph(retriever: LaureateRetriever):
    """Wire the steps together. Each step gets the state and returns the fields it adds."""

    # ---- step 1: the model chooses the tool(s) -----------------------------
    def choose_tool(state: State) -> State:
        trace, ask = state["trace"], _ask_with_previous(state)
        try:
            reply = _tool_chooser().invoke([SystemMessage(TOOL_PROMPT.format(today=date.today())),
                                            HumanMessage(ask)])
            calls = reply.tool_calls
        except Exception as err:  # Ollama error or unreadable tool call
            trace.error("choose_tool", err)
            calls = []
        trace.add("choose_tool", tool_calls=[{"tool": c["name"], "args": c["args"]} for c in calls])

        search = [c for c in calls if c["name"] == "search_laureate_articles"]
        table = [c for c in calls if c["name"] == "query_nobel_table"]
        plan = None
        if not calls:
            # No tool called: decide with keywords, the way the bot used to.
            match = _TABLE_QUESTION.search(state["question"])
            trace.add("choose_tool.fallback", reason="model called no tool; keyword rule used",
                      route="table" if match else "text", matched=match.group(0) if match else None)
            if match:
                plan = _fallback_plan(ask, trace)
            search = [] if plan else [{"args": {}}]
        elif table:
            try:  # the tool's arguments are the form itself
                plan = TablePlan(queries=[q for c in table for q in TablePlan.model_validate(c["args"]).queries][:3])
            except ValidationError as err:
                trace.error("plan.raw", err)
                plan = _fallback_plan(ask, trace)
            if not plan and not search:  # no usable form at all: search the articles instead
                trace.add("plan.fallback", reason="no valid form; using article search instead")
                search = [{"args": {}}]

        update: State = {"search": bool(search),
                         "model_query": (search[0]["args"].get("query") or "") if search else ""}
        if plan:
            raw = [q.model_dump(exclude_defaults=True) for q in plan.queries]
            trace.add("plan.raw", queries=raw)
            plan = fix_plan(plan, state["question"])  # repair common form-filling slips
            fixed = [q.model_dump(exclude_defaults=True) for q in plan.queries]
            trace.add("plan.fixed", changed=fixed != raw, queries=fixed)
            update["plan"] = plan
        update["route"] = "+".join(r for r, used in (("text", search), ("table", plan)) if used)
        return update

    # ---- tool: article search ---------------------------------------------
    def retrieve(state: State) -> State:
        question, history = state["question"], state["history"]
        # The search uses the user's own words: the model sometimes adds a
        # name from its own memory ("James Watson" for a DNA question), which
        # would hide the other laureates. Only follow-ups that name nobody
        # ("When did she die?") use the model's rewrite, which replaces the
        # pronoun; if that has no name either, the previous question is added.
        previous = [m["content"] for m in history if m["role"] == "user"]
        query = question
        if previous and not retriever.names.find(question) and _PRONOUNS.search(question):
            rewrite = state.get("model_query", "")
            query = rewrite if retriever.names.find(rewrite) else f"{previous[-1]} {question}"
        result = retriever.retrieve(query)
        context, sources = format_context(result.docs)
        state["trace"].add(
            "retrieve", search_query=query, names_found=retriever.names.find(query),
            retrieval_route=result.route, laureates=result.laureates, prize_filter=result.category,
            docs=[f"{d.metadata['score']:.3f} {d.metadata['found_by']}: {d.metadata['name']} — "
                  f"{d.metadata.get('heading', 'profile')}" for d in result.docs],
            context_chars=len(context), docs_in_context=len(sources))
        return {"search_query": query, "retrieval": result, "sources": sources, "context": context}

    # ---- tool: counting table ---------------------------------------------
    def run_table(state: State) -> State:
        return {"table_result": run_plan(state["plan"], state["trace"])}

    # ---- step 3: the prompt for the answer ---------------------------------
    def build_prompt(state: State) -> State:
        context, results = state.get("context", ""), state.get("table_result", "")
        if context and results:
            system = BOTH_PROMPT.format(results=results, context=context)
        elif results:
            system = TABLE_PROMPT.format(results=results)
        else:
            system = TEXT_PROMPT.format(context=context)
        return {"messages": [SystemMessage(system), *_history_messages(state["history"]),
                             HumanMessage(state["question"])]}

    graph = StateGraph(State)
    graph.add_node("choose_tool", choose_tool)
    graph.add_node("retrieve", retrieve)
    graph.add_node("run_table", run_table)
    graph.add_node("build_prompt", build_prompt)
    graph.add_edge(START, "choose_tool")
    graph.add_conditional_edges("choose_tool", lambda s: "retrieve" if s["search"] else "run_table",
                                ["retrieve", "run_table"])
    graph.add_conditional_edges("retrieve", lambda s: "run_table" if s.get("plan") else "build_prompt",
                                ["run_table", "build_prompt"])
    graph.add_edge("run_table", "build_prompt")
    graph.add_edge("build_prompt", END)
    return graph.compile()


_graphs: dict[int, object] = {}


def prepare_turn(question: str, history: list[dict], retriever: LaureateRetriever) -> Turn:
    """Run the graph. history = [{"role": "user"|"assistant", "content": str}, ...]"""
    if id(retriever) not in _graphs:  # compile once per retriever
        _graphs[id(retriever)] = build_graph(retriever)
    trace = Trace(question)
    if history:
        trace.add("history", previous_question=[m["content"] for m in history if m["role"] == "user"][-1:])
    try:
        s = _graphs[id(retriever)].invoke({"question": question, "history": history, "trace": trace})
    except Exception as err:
        trace.error("graph", err)
        trace.finish("(failed before answering)")
        raise
    trace.add("prompt", route=s["route"], messages=len(s["messages"]),
              prompt_chars=sum(len(m.content) for m in s["messages"]))
    return Turn(question=question, route=s["route"], messages=s["messages"],
                search_query=s.get("search_query", ""), retrieval=s.get("retrieval"),
                sources=s.get("sources", []), plan=s.get("plan"), table_result=s.get("table_result", ""),
                trace=trace)


def stream_answer(llm, turn: Turn) -> Iterator[tuple[str, str]]:
    """
    Yield ("thinking", text) and ("answer", text) pieces as the model
    produces them, so the UI can show the answer word by word.
    """
    for chunk in llm.stream(turn.messages):
        thought = chunk.additional_kwargs.get("reasoning_content")
        if thought:
            yield "thinking", thought
        if chunk.content:
            yield "answer", chunk.content


# ===========================================================================
# Terminal check: python -m chatbot.chat "question"
# ===========================================================================
if __name__ == "__main__":
    from chatbot.retriever import source_label

    question = " ".join(sys.argv[1:]) or "Who discovered the structure of DNA?"
    turn = prepare_turn(question, [], LaureateRetriever())
    print(f"Q: {question}   (route: {turn.route})\n")
    if turn.plan:
        print("Table queries:", [q.model_dump(exclude_defaults=True) for q in turn.plan.queries], "\n")
    answer = ""
    for kind, text in stream_answer(get_llm(), turn):
        if kind == "answer":
            answer += text
            print(text, end="", flush=True)
    print()
    for i, doc in enumerate(turn.sources, 1):
        print(f"  [{i}] {source_label(doc)}  {doc.metadata['url']}")
    turn.trace.finish(answer)
    print("\n--- trace (also in logs/) ---\n" + turn.trace.as_text())
