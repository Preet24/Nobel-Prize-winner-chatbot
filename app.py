"""
Streamlit chat app for the Nobel laureate data.

Run from the project root (Ollama must be running):
    D:\\python_evenv\\Scripts\\streamlit.exe run app.py

Streamlit re-runs this whole script on every interaction (each message,
each toggle). So:
  * heavy objects (vector store, model) are created once with @st.cache_resource
  * the conversation lives in st.session_state, which survives the re-runs
"""

import streamlit as st

from chatbot.chat import prepare_turn, stream_answer
from chatbot.config import CHAT_MODEL
from chatbot.llm import get_llm
from chatbot.retriever import LaureateRetriever, source_label

st.set_page_config(page_title="Nobel Laureate Chat", page_icon="🏅")


# ---------------------------------------------------------------------------
# Loaded once per server, shared by all re-runs
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner="Loading the laureate database…")
def load_retriever() -> LaureateRetriever:
    return LaureateRetriever()


@st.cache_resource
def load_llm():
    # Thinking stays off: with the tools the answers are better and much faster.
    return get_llm(thinking=False)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Settings")
    show_details = st.toggle(
        "Show retrieval details",
        value=False,
        help="Which route was used and how close each source was (smaller = closer).",
    )
    show_debug = st.toggle(
        "Show debug log",
        value=False,
        help="Every step of each answer: chosen tool, the form the model filled in, "
             "laureates left after each filter. Also saved in logs/chatbot.log and logs/turns.jsonl.",
    )
    if st.button("Clear chat", use_container_width=True):
        st.session_state.messages = []
        st.rerun()
    st.caption(
        f"Model: `{CHAT_MODEL}` via Ollama (thinking off). For each question the "
        "model picks a tool: article search over the 1017 Wikipedia articles in "
        "`laureates_data/`, or the counting table built from the official Nobel "
        "Prize data (\"how many…\", \"youngest…\")."
    )


# ---------------------------------------------------------------------------
# Showing one message (used for history and for the new answer)
# ---------------------------------------------------------------------------
def show_sources(sources: list[dict], details: dict | None):
    """sources: [{"label", "url", "score", "found_by"}] in citation order [1], [2], ..."""
    if not sources:
        return
    with st.expander(f"Sources ({len(sources)})"):
        for i, s in enumerate(sources, 1):
            extra = f" · {s['found_by']}, {s['score']:.3f}" if details else ""
            st.markdown(f"**[{i}]** [{s['label']}]({s['url']}){extra}")
        if details:
            st.caption(
                f"Route {details['route']} · laureates searched: {', '.join(details['laureates'])}"
                + (f" · prize filter: {details['category']}" if details["category"] else "")
                + (f"  \nSearch text: “{details['query']}”" if details["query"] != details["question"] else "")
            )


def show_table_lookup(table: dict | None):
    """Counting questions: show the filled-in query form(s) and the exact table result."""
    if not table:
        return
    with st.expander("Table lookup (official Nobel Prize data)"):
        for q in table["queries"]:
            st.code(", ".join(f"{k} = {v}" for k, v in q.items()), language=None)
        st.text(table["result"])


def show_evidence(message: dict):
    show_table_lookup(message.get("table"))
    show_sources(message.get("sources", []), message.get("details") if show_details else None)
    if show_debug and message.get("trace"):
        with st.expander("Debug log"):
            st.code(message["trace"], language=None)


def render(message: dict):
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if message["role"] == "assistant":
            show_evidence(message)


# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------
st.title("🏅 Nobel Laureate Chat")

if "messages" not in st.session_state:
    st.session_state.messages = []

if not st.session_state.messages:
    st.info(
        "Ask about any Nobel laureate, for example:\n"
        "- *What did Abdus Salam work on?*\n"
        "- *Who discovered the structure of DNA?*\n"
        "- *Which Peace Prize laureates campaigned for women's education?*\n"
        "- *How many laureates were born in Spain?*\n"
        "- *Who is the third youngest laureate?*"
    )

for message in st.session_state.messages:
    render(message)

question = st.chat_input("Ask about a Nobel laureate…")
if question:
    history = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages]
    user_msg = {"role": "user", "content": question}
    st.session_state.messages.append(user_msg)
    render(user_msg)

    with st.chat_message("assistant"):
        try:
            # 1) The model picks a tool (article search or counting table),
            #    and the tool gathers the facts.
            with st.spinner("Choosing a tool and looking it up…"):
                turn = prepare_turn(question, history, load_retriever())

            # 2) Generation: stream the answer piece by piece.
            answer_area = st.empty()
            answer_area.markdown("_Writing the answer…_")
            answer = ""
            for kind, text in stream_answer(load_llm(), turn):
                if kind == "answer":
                    answer += text
                    answer_area.markdown(answer + "▌")  # cursor while writing
            answer_area.markdown(answer or "_The model returned an empty answer._")
            turn.trace.finish(answer)  # write the full turn to logs/turns.jsonl
        except Exception as err:  # most often: Ollama is not running
            if "turn" in locals():
                turn.trace.error("answer", err)
                turn.trace.finish("(failed while answering)")
            st.error(f"Something went wrong: {err}\n\nIs Ollama running? Try `ollama serve`. "
                     "Details are in logs/chatbot.log.")
            st.stop()

        # 3) Keep everything needed to redraw this answer after the next re-run.
        r = turn.retrieval  # None when the counting table was used
        assistant_msg = {
            "role": "assistant",
            "content": answer,
            "sources": [
                {"label": source_label(d), "url": d.metadata["url"],
                 "score": d.metadata["score"], "found_by": d.metadata["found_by"]}
                for d in turn.sources
            ],
            "details": r and {"route": r.route, "laureates": r.laureates, "category": r.category,
                              "query": turn.search_query, "question": question},
            "table": turn.plan and {
                "queries": [q.model_dump(exclude_defaults=True) for q in turn.plan.queries],
                "result": turn.table_result,
            },
            "trace": turn.trace.as_text(),
        }
        show_evidence(assistant_msg)
    st.session_state.messages.append(assistant_msg)
