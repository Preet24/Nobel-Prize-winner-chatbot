# Nobel Laureate Chatbot

A chatbot that answers questions about Nobel Prize laureates. It runs entirely
on your own computer: a small local language model (`qwen3.5:4b` through
Ollama), a vector database (ChromaDB), and a Streamlit web page. There are no
API keys and no cloud services.

It can answer two kinds of questions:

| Kind of question | Example | How it is answered |
|---|---|---|
| Facts about a laureate or a topic | "What did Abdus Salam work on?", "Who discovered the structure of DNA?" | Searches 1017 Wikipedia articles and answers with numbered citations `[1]` |
| Counting, ranking, complete lists | "How many laureates were born in Spain?", "Who is the third youngest laureate?" | Looks the answer up exactly in a table built from the official Nobel Prize API |

The model decides which of the two tools to use for each question. It can also
use both, for example "How many women won Physics, and what did they discover?".

More reading:
- [AGENTIC_AI.md](AGENTIC_AI.md): what "agentic AI" means and how this chatbot uses tools.
- [VECTOR_EMBEDDING.md](VECTOR_EMBEDDING.md): how the article search works (embeddings, the two layers, retrieval).
- [CLAUDE.md](CLAUDE.md): a detailed technical reference for every module.

---

## 1. What you need

- **Windows** with PowerShell (the commands below use Windows paths).
- **Python 3.12** in the virtual environment at `D:\python_evenv`. It already has
  streamlit, langchain, langchain-ollama, langchain-chroma, langgraph, chromadb,
  pandas and pydantic installed.
- **[Ollama](https://ollama.com)** installed and running, with two models:
  ```powershell
  ollama pull qwen3.5:4b         # the chat model (fits a 4 GB GPU, partly on CPU)
  ollama pull nomic-embed-text   # turns text into vectors for the search
  ```

For a new environment, install the packages with:
```powershell
D:\python_evenv\Scripts\python.exe -m pip install streamlit langchain langchain-ollama langchain-chroma langgraph chromadb pandas pydantic
```

## 2. Quick start

All commands are run from the project folder:

```powershell
cd "D:\ML dataset\Courses\Nobel_Prize_analysis_with_claude"
D:\python_evenv\Scripts\streamlit.exe run app.py
```

Open http://localhost:8501 in your browser and ask a question.

This works straight away because the built files (`chroma_db/`,
`laureate_prizes.csv`, …) are already in the folder. To rebuild them from
scratch, see section 4.

**Speed:** on a GTX 1650 (4 GB), the first answer takes 1–2 minutes while the
model loads. Later answers take about 15–70 seconds.

## 3. Project layout

```
app.py                     Streamlit chat page
eval_chat.py               35 test questions -> eval_results.md
chatbot/
  config.py                every path, model name and setting in one place
  llm.py                   get_llm(): the only place that creates the chat model
  cleaning.py              loads a laureate JSON file, cleans the text, finds the prize
  embeddings.py            nomic-embed-text with its search_document / search_query prefixes
  ingest.py                builds the two-layer vector database (chroma_db/)
  retriever.py             finds the right articles and sections for a question
  build_facts.py           builds laureate_prizes.csv from the official Nobel API
  table_tool.py            the counting tool: a fixed query form run with pandas
  chat.py                  the LangGraph agent: choose tool -> run tool -> answer
  tracing.py               step-by-step log of every chat turn (logs/)
  extract_details.py       reads birthplace, parents, education... from article text
laureates_data/            1017 Wikipedia articles as JSON (the source data)
prize_overrides.csv        hand-checked prizes from api.nobelprize.org (29 laureates)
nobel_api_laureates.json   cached download of the official Nobel API
laureate_prizes.csv        one row per prize: the table the counting tool queries
laureates.csv              one row per laureate from the Wikipedia data
laureate_details.csv       details extracted from article text (extract_details.py)
chroma_db/                 the vector database
logs/                      chatbot.log and turns.jsonl
```

## 4. Building the data from scratch

Ollama must be running for steps 2 and 4.

```powershell
# 1. Optional check: how many prizes the cleaner can find (writes nothing)
D:\python_evenv\Scripts\python.exe -m chatbot.cleaning

# 2. Vector database: about 12,000 embeddings (several minutes)
D:\python_evenv\Scripts\python.exe -m chatbot.ingest

# 3. Counting table from the official Nobel API (add --refresh to re-download)
D:\python_evenv\Scripts\python.exe -m chatbot.build_facts

# 4. Optional: extra details from article text (8-12 hours; stop with Ctrl+C and rerun to resume)
D:\python_evenv\Scripts\python.exe -m chatbot.extract_details --limit 5   # try 5 first
D:\python_evenv\Scripts\python.exe -m chatbot.extract_details
```

## 5. Other useful commands

```powershell
# One answer in the terminal, with the tool chosen and the step-by-step log
D:\python_evenv\Scripts\python.exe -m chatbot.chat "How many laureates were born in Spain?"

# What the article search returns for a question (no chat model needed)
D:\python_evenv\Scripts\python.exe -m chatbot.retriever "Who discovered penicillin?"

# Sample counting queries (no chat model needed)
D:\python_evenv\Scripts\python.exe -m chatbot.table_tool

# Run all 35 test questions (about 15-30 min) or only some of them
D:\python_evenv\Scripts\python.exe eval_chat.py
D:\python_evenv\Scripts\python.exe eval_chat.py 25 33

# Show the log of the last 3 chat turns
D:\python_evenv\Scripts\python.exe -m chatbot.tracing 3
```

Always run modules with `-m` from the project folder, because they import each
other as `chatbot.something`.

## 6. How it works (short version)

```
question
   │
   ▼
choose_tool ── the model reads the question and calls a tool
   │
   ├── search_laureate_articles ──► two-layer vector search over the Wikipedia articles
   │                                  layer 1: one profile per laureate
   │                                  layer 2: ~12k article sections
   │
   └── query_nobel_table ─────────► fills a fixed form (count / list / youngest / ...)
                                      which pandas runs on laureate_prizes.csv
   │
   ▼
build_prompt ── the facts found + rules ("answer only from these, cite [n]")
   │
   ▼
the model writes the answer, streamed word by word
```

- The model **never writes code**. For counting it only fills in a form, and
  ordinary Python runs the form exactly. A small model can't count reliably,
  but it can fill in a form.
- **Safety nets:** if the model calls no tool, a keyword rule picks one. If the
  form it fills is invalid, a second, stricter call fills it again. A repair
  step (`fix_plan`) fixes common slips, such as a city written in the country field.
- **Thinking is off.** qwen3.5 can "think" before answering. With tools,
  answers were both better and several times faster without thinking.

[AGENTIC_AI.md](AGENTIC_AI.md) explains each of these ideas in more detail.

## 7. Test results

`eval_chat.py` asks 35 questions through the same code path as the app and
checks each answer for the facts it must contain. The latest run passed 34 of 35.
The one failure ("Who won Physics in 2031?") answered "no laureates", which is
also correct, so the test now accepts that answer as well. Every
question, answer and verdict is written to `eval_results.md`.

## 8. Known limits

- **It's slow on a small GPU.** About half of the 4B model runs on the CPU.
  Ollama occasionally stalls for several minutes. That's Ollama, not a bug in the code.
- **Education counts depend on Wikipedia infoboxes.** They're often incomplete
  (e.g. "Oxford, all degree levels" comes out lower than the true number).
  `extract_details.py` fills these gaps from the article text, but its output
  isn't connected to the counting tool yet.
- **108 laureates have no article text,** only an infobox (e.g. Aage Bohr).
  Questions about them can only use the short profile.
- **Answers come only from this data.** The bot says it couldn't find something
  rather than guessing. Questions outside the data, such as "What is the capital
  of France?", are refused.

## 9. Debugging a wrong answer

Turn on **"Show debug log"** in the app's sidebar, or run
`python -m chatbot.tracing`. For each turn, the log shows:
- which tool the model called, and with which arguments;
- the table form before and after `fix_plan`;
- how many laureates were left after each filter;
- which article sections the search returned.

Usually one of these lines shows where it went wrong.
