# Agentic AI, explained with this chatbot

This file explains what "agentic AI" means in plain language, using the Nobel
chatbot as the running example. Each idea is followed by **where it lives in
the code**, so you can open the file and see it.

For how the article search itself works (embeddings, vectors, the two layers),
see [VECTOR_EMBEDDING.md](VECTOR_EMBEDDING.md).

---

## 1. From chatbot to agent: four levels

Think of a language model (LLM) as a very well-read person locked in a room
with no books. It answers from memory, which can be out of date, incomplete,
or simply made up. "Agentic" systems give it more and more ability to act:

| Level | What the model does | Weakness |
|---|---|---|
| **1. Plain chatbot** | Answers from memory | Invents facts ("hallucinates"), can't cite anything |
| **2. RAG** (Retrieval-Augmented Generation) | Our code first finds relevant text and pastes it into the prompt. The model answers from that text | The same search runs for every question, even when searching is the wrong method (it can't count) |
| **3. Tool-using model** | The model **chooses** which tool to use and fills in its arguments. Our code runs the tool | The model makes one decision, then answers |
| **4. Full agent (loop)** | The model calls a tool, reads the result, decides on the next tool, and repeats until it's done | More steps → slower, and more chances for a small model to go off track |

**This chatbot is level 3:** a tool-using model inside a fixed workflow.
Section 7 explains why it doesn't use the full loop.

A useful definition: *an agent is an LLM that decides what to do next, not
just what to say next.* Here, the decision is "which tool, with which inputs".

---

## 2. Why this chatbot needs to make a choice at all

The first version was pure RAG (level 2), and it failed on questions like:

> "How many Nobel laureates were born in Spain?"

Vector search returns the **4–6 most similar text passages**, not *all*
matching laureates. The model then counted the few it was shown and gave a
confident wrong number. Searching and counting are different jobs:

| Job | Right tool | Why |
|---|---|---|
| "What did Salam work on?" | **Search** the articles | The answer is a passage of text |
| "How many were born in Spain?" | **Count** rows in a table | The answer needs *every* laureate, checked exactly |

So the model is given both tools and decides which one each question needs.

---

## 3. Tools: what the model actually sees

A "tool" is a function our code can run. The model never runs it. It only
sees a **name, a description and a list of arguments**, and replies with a
request: "please call tool X with these arguments".

The bot has two tools, defined as pydantic classes in `chatbot/chat.py`.
The class name becomes the tool name, the docstring becomes the description,
and the fields become the arguments:

```python
class search_laureate_articles(BaseModel):
    """Search the Wikipedia articles about Nobel laureates. Use for a laureate's
    life, work, discoveries, prizes, or who did something."""
    query: str = Field(description="What to search for. ...")

class query_nobel_table(TablePlan):
    """Look up the official Nobel Prize data table of all laureates. Use for
    counting, ranking (youngest/oldest), complete lists, or comparing countries."""
```

LangChain turns these classes into a JSON description and sends it to Ollama
along with the question (`bind_tools`). The model replies with something like:

```json
{"name": "query_nobel_table",
 "args": {"queries": [{"action": "count", "birth_country": "Spain"}]}}
```

**The descriptions are the instructions.** If the model picks the wrong tool,
the first thing to change is the docstring or the tool prompt, not the code.

**Where:** `search_laureate_articles`, `query_nobel_table` and `_tool_chooser()` in `chatbot/chat.py`.

---

## 4. Structured output: the model fills a form, it never writes code

An obvious design is "let the model write pandas code, then run it". This bot
deliberately doesn't do that:
- a 4B model writes buggy code;
- running code that a model wrote is a security risk;
- wrong code produces a confident wrong number, with no way to see why.

Instead, the counting tool's arguments are a **fixed form** (`TableQuery` in
`chatbot/table_tool.py`):

| Field | Example | Meaning |
|---|---|---|
| `action` | `count`, `list`, `youngest`, `oldest`, `count_by_country`, `count_by_category` | What to do |
| `category` | `Physics` | Prize category |
| `birth_country` / `birth_place` | `Spain` / `New York` | Where born |
| `university`, `undergraduate_only` | `Harvard`, `true` | Where they studied |
| `gender`, `organizations` | `female`, `true` | Women only / organisations only |
| `year_from`, `year_to` | `2001` | Prize year range |
| `rank` | `3` | "third youngest" |

One question can ask for up to 3 queries ("the third youngest **and** the
oldest" → two queries). Ordinary, tested Python code (`run_query`) runs each
query on `laureate_prizes.csv` and returns exact numbers.

The model's job shrinks from "solve the problem" to "fill in the boxes", which
small models do well. Because the form is a pydantic schema, anything that
doesn't fit (a misspelt action, a word where a number belongs) is rejected
automatically instead of producing a wrong answer.

This is called **structured output**. `extract_details.py` uses the same idea
(the `Family` and `Education` forms) to pull facts out of article text.

**Where:** `TableQuery`, `TablePlan` and `run_query` in `chatbot/table_tool.py`.

---

## 5. The workflow: a LangGraph state graph

LangGraph describes the agent as a **graph**: boxes (nodes) are steps, and
arrows (edges) say which step comes next. A shared **state** dictionary
travels through the graph, and each step adds its results to it.

```
          START
            │
            ▼
      ┌─────────────┐   one LLM call with both tools attached
      │ choose_tool │   → sets: search? plan? route
      └─────────────┘
         │        │
 search  │        │ table only
         ▼        │
   ┌──────────┐   │
   │ retrieve │   │     two-layer vector search
   └──────────┘   │
     │      │     │
     │ both │     │
     │      ▼     ▼
     │   ┌───────────┐
     │   │ run_table │    pandas runs the filled form
     │   └───────────┘
     │         │
     ▼         ▼
   ┌──────────────┐
   │ build_prompt │      picks TEXT_PROMPT / TABLE_PROMPT / BOTH_PROMPT
   └──────────────┘
            │
           END  → stream_answer() writes the reply word by word
```

The arrows leaving `choose_tool` and `retrieve` are **conditional edges**:
small functions that read the state and return the name of the next node:

```python
graph.add_conditional_edges("choose_tool", lambda s: "retrieve" if s["search"] else "run_table", ...)
graph.add_conditional_edges("retrieve",    lambda s: "run_table" if s.get("plan") else "build_prompt", ...)
```

**The state** (class `State` in `chat.py`) holds the question, history, the
chosen route, the search results, the table form and its result, and the
final prompt messages. Every step can see what earlier steps found.

**Why a graph instead of one long function?** Each step is small and can be
tested on its own. The flow is drawn in one place, and adding a new tool means
adding one node and one arrow.

**Where:** `build_graph()` in `chatbot/chat.py`.

---

## 6. Guardrails: what to do when the model gets it wrong

A small model makes mistakes. A good agent design expects this and has a plan
for each kind of slip. These are the safety nets in this bot, in the order
they apply:

| Problem | What happens | Where |
|---|---|---|
| The model calls **no tool** | A keyword rule (`_TABLE_QUESTION`: "how many", "youngest", …) picks the route | `choose_tool` |
| The table form is **invalid** (fails the schema) | A second call with a strict JSON schema (`_fallback_plan`) fills the form again | `choose_tool`, `_planner()` |
| Still no valid form | Falls back to article search, so the user always gets an answer | `choose_tool` |
| The form has **common slips** (a city in `birth_country`, a year filter the question never mentioned, "undergrad" not ticked) | `fix_plan` repairs them with simple rules | `table_tool.py` |
| The model **adds names from memory** to the search ("James Watson" for a DNA question, which hid Crick) | The search uses the user's own words, and only uses the model's rewrite for "he/she" follow-ups | `retrieve` |
| The model **invents facts** in the answer | The prompt says "answer ONLY from the sources, cite [n], say if not found"; table numbers are marked "exact, do not recount" | `TEXT_PROMPT`, `TABLE_PROMPT` |
| The model **makes up details** while extracting | `verify` drops any value whose words don't appear in the article text | `extract_details.py` |

The general rule: **let the model decide, let code check.** Anything that
code can do exactly (counting, matching, checking a word is really in the
text) is done by code.

---

## 7. Why not a full agent loop?

A full agent (level 4, often called **ReAct**: *Reason + Act*) works like this:

```
think → call a tool → read the result → think → call another tool → ... → answer
```

It's more flexible. It could, for example, search the articles and then
decide to count something it read about. This bot uses a fixed graph instead
because of the hardware:

- **Speed:** each model call takes 10–60 s on a 4 GB GPU. A loop of 3–5 calls
  would take minutes per answer.
- **Reliability:** a 4B model in a long loop tends to repeat a tool, forget
  the goal, or stop early. One focused decision is much more reliable.
- **Coverage:** nearly every question here needs one lookup, or one of each
  (the "both tools" route already covers that).

With a larger model (for example Claude through the API), switching to a loop
would be a small change. LangGraph supports it directly by adding an edge from
the tool nodes back to `choose_tool`. The only file that creates the model is
`chatbot/llm.py`.

---

## 8. Thinking mode

qwen3.5 can "think out loud" (reasoning) before it answers. You'd expect that
to help, but in testing with tools, answers were **better with thinking
off**, and thinking made each answer several times **slower** on this GPU.
So thinking is always off (`get_llm(thinking=False)`).

The lesson: for a small model, **clear tools and a few examples in the
prompt** (the "Table examples" in `TOOL_PROMPT`, called **few-shot
prompting**) help more than extra reasoning.

---

## 9. Memory: follow-up questions

The model has no memory between calls, so every call is sent what it needs:

- **To choose a tool,** it gets the previous question too: "Previous question:
  How many were born in Spain? Question: And how many in Italy?" This lets it
  fill the form for Italy.
- **For the search,** a follow-up like "When did she die?" names nobody, so
  the bot uses the model's rewritten query ("When did Marie Curie die?") if it
  contains a laureate's name. Otherwise it searches with the previous
  question plus the new one.
- **For the answer,** the last `HISTORY_TURNS` (2) question/answer pairs are
  included in the prompt.

This is **short-term memory**: it lasts for one chat. Nothing is stored
between sessions.

---

## 10. Observability: seeing what the agent did

When an agent gives a wrong answer, you need to know which step went wrong:
the tool choice, the form, the filter, or the final writing. `tracing.py`
records every step of every turn (simplified example):

```
choose_tool   tool_calls=[{tool: query_nobel_table, args: {queries: [{action: count, birth_place: New York}]}}]
plan.raw      [{action: count, birth_place: New York}]
plan.fixed    changed=False
table.filter  birth_place=New York -> 74 laureates
table.result  ...
prompt        route=table, prompt_chars=...
answer        "74 laureates were born in New York state..."
```

Logs go to `logs/chatbot.log` (readable) and `logs/turns.jsonl` (one JSON
record per turn). In the app, turn on **"Show debug log"**. In the terminal,
run `python -m chatbot.tracing 3`.

`eval_chat.py` is the other half of observability. Its 35 fixed questions
with expected facts are rerun after every change, so an improvement for one
question can't silently break another.

---

## 11. Glossary

| Term | Meaning |
|---|---|
| **LLM** | Large language model: predicts text. Here: `qwen3.5:4b` running locally in Ollama |
| **Agent** | An LLM that decides which action to take next, not only what to say |
| **Tool / function calling** | The model replies with "call tool X with arguments Y" instead of text; our code runs it |
| **RAG** | Retrieval-Augmented Generation: find relevant text first, then answer from it |
| **Structured output** | Forcing the model's reply to match a schema (a form), checked by pydantic |
| **Schema** | The exact shape the data must have: field names, types, allowed values |
| **State graph** | LangGraph's way of writing a workflow: nodes (steps), edges (what's next), shared state |
| **Conditional edge** | An arrow whose target is chosen at run time by looking at the state |
| **ReAct loop** | Reason → Act → observe → repeat: a full agent that can call many tools in a row |
| **Guardrail / fallback** | Code that catches a model mistake and repairs it or takes a safe path |
| **Few-shot prompting** | Putting a few worked examples in the prompt so the model copies the pattern |
| **Hallucination** | A fluent but made-up answer |
| **Tracing** | Recording each step an agent takes, for debugging |
| **Evaluation (eval)** | A fixed set of test questions with expected answers, rerun after every change |

---

## 12. Where to look in the code

| Idea | File | Name |
|---|---|---|
| Tool definitions | `chatbot/chat.py` | `search_laureate_articles`, `query_nobel_table` |
| Tool choice prompt (few-shot) | `chatbot/chat.py` | `TOOL_PROMPT` |
| Attaching tools to the model | `chatbot/chat.py` | `_tool_chooser()` |
| The graph | `chatbot/chat.py` | `build_graph()` |
| Fallbacks | `chatbot/chat.py` | `choose_tool`, `_fallback_plan` |
| The form the model fills | `chatbot/table_tool.py` | `TableQuery`, `TablePlan` |
| Repairing the form | `chatbot/table_tool.py` | `fix_plan` |
| Running the form exactly | `chatbot/table_tool.py` | `run_query` |
| The search tool | `chatbot/retriever.py` | `LaureateRetriever.retrieve` |
| The model itself | `chatbot/llm.py` | `get_llm()` |
| Step-by-step logs | `chatbot/tracing.py` | `Trace` |
| Tests | `eval_chat.py` | `CASES` |
