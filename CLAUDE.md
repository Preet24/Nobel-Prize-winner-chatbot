# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository state

This project is a RAG chatbot over Nobel Prize laureates. It has no git repository or tests. The source data is `laureates_data/`, which holds 1017 JSON files scraped from English Wikipedia, one per laureate (people and organizations, e.g. `European_Union.json`). Each file is named after the Wikipedia article title with underscores (`Aage_Bohr.json`).

`VECTOR_EMBEDDING.md` (the article search) and `AGENTIC_AI.md` (tools, the LangGraph graph, fallbacks) are plain-language explanations of the design, written for the user. `README.md` covers setup and commands. Keep all three in sync when the design changes.

## Commands

```powershell
D:\python_evenv\Scripts\python.exe -m chatbot.cleaning   # prize-extraction stats; writes nothing
D:\python_evenv\Scripts\python.exe -m chatbot.ingest     # rebuild chroma_db/ + laureates.csv (~12k embeddings; needs Ollama running)
D:\python_evenv\Scripts\python.exe -m chatbot.retriever ["question"]  # print what retrieval returns (demo questions if none given)
D:\python_evenv\Scripts\python.exe -m chatbot.build_facts [--refresh] # laureate_prizes.csv from the cached Nobel API download (--refresh re-downloads)
D:\python_evenv\Scripts\python.exe -m chatbot.table_tool              # sample counting queries, no LLM
D:\python_evenv\Scripts\python.exe -m chatbot.extract_details [--limit N | Stem ... | --redo | --csv-only]  # article text -> laureate_details.csv (~8-12 h for all; resumes)
D:\python_evenv\Scripts\python.exe -m chatbot.chat "question"         # one full answer in the terminal (shows route + table queries)
D:\python_evenv\Scripts\python.exe eval_chat.py [case numbers]        # 35 test questions -> eval_results.md (~15 min)
D:\python_evenv\Scripts\python.exe -m chatbot.tracing [n]             # print the step-by-step log of the last n chat turns
D:\python_evenv\Scripts\streamlit.exe run app.py                      # the chat app, http://localhost:8501
```

Run them as modules (`-m`) from the project root, because they use package imports (`from chatbot.config import ...`).

## Architecture

The pipeline is: `cleaning.py` → `ingest.py` → `chroma_db/` → `retriever.py` → `chat.py` → `app.py` (Streamlit). Counting questions go through a separate path: `build_facts.py` → `laureate_prizes.csv` → `table_tool.py`.

- `chatbot/config.py` holds every path, model name and chunk size.
- `chatbot/cleaning.py`:
  - `load_record` cleans the text and the infobox keys.
  - `extract_prizes` looks for the prize in the infobox first, then the intro (first mention only), then the body (first mention only).
  - `prize_overrides.csv` replaces the extracted prizes entirely for listed `file_stem`s. These rows are hand-checked against `api.nobelprize.org`, and each row links to its source.
- `chatbot/embeddings.py` defines `NomicEmbeddings`, which adds the `search_document: ` / `search_query: ` prefixes. Ingestion and retrieval must both use it.
- `chatbot/ingest.py` builds two Chroma collections, both using cosine distance:
  - `laureate_profiles`: one profile per laureate (layer 1)
  - `laureate_sections`: about 12k chunks, each prefixed with `"<Name> — <Heading>: "` (layer 2)
  - Every run deletes and rebuilds both collections. Chunk IDs look like `Stem::s3::c1`.
- `chatbot/retriever.py`:
  - `LaureateRetriever.retrieve(q)` returns a `RetrievalResult`.
  - `NameIndex.find` matches full names, first + last names, `ALIASES`, and surnames shared by at most `MAX_NAMED` laureates. Organizations are detected by having no `birth_year`, and they get no surname key.
  - Surnames that are common lower-case words need a capital letter.
  - Route A (a laureate is named) returns the profile plus that laureate's chunks. Route B searches profiles (optionally with a `won_*` filter from `find_category`), then chunks, with at most `MAX_CHUNKS_EACH` per laureate.
  - Every query also gets an unfiltered safety-net search. In Route A, it keeps only chunks that contain the named surname (case-sensitive).
  - `format_context` numbers the sources and caps the text at `MAX_CONTEXT_CHARS`.
  - Retrieval settings are in `config.py`.
- `chatbot/llm.py`: `get_llm(thinking=False)` returns a `ChatOllama` with `num_ctx=CHAT_NUM_CTX`. It's the only place to change for another model or provider. qwen3.5 thinks by default; `reasoning=False` turns that off, which is much faster.
- `chatbot/build_facts.py`:
  - Downloads all laureates from `api.nobelprize.org` (paged, with a User-Agent) into `nobel_api_laureates.json` once. Later runs use that cache.
  - Writes `laureate_prizes.csv`: one row per prize (1036 rows, 1028 laureates). The base data is the official API, so counts cover all laureates, not just our 1017 files.
  - `birth_country` uses today's country names. Scotland and Northern Ireland appear separately from the United Kingdom.
  - `age_at_award` is exact, from the birth date to `dateAwarded`.
  - The Wikipedia Education/Alma mater fields are joined in. They're matched by Wikipedia URL, then by the API ids in `prize_overrides.csv`, then by family name plus prize year (1014 of 1028 match).
  - `undergrad_schools` is set only when a bachelor's degree marker (BA/AB/BS/BSc/SB/LLB/MB…) follows the school.
- `chatbot/extract_details.py` fills infobox gaps from the article text. It is not yet joined into `laureate_prizes.csv` or the table tool.
  - `bio_text` = Introduction + early life/education/family/personal sections, each section given an equal share of `MAX_TEXT_CHARS`.
  - Two `with_structured_output(..., method="json_schema")` calls at temperature 0: `Family` (birthplace, citizenship, parents, religion, spouses, children) and `Education` (schools + degrees). One combined form made qwen3.5:4b skip fields.
  - `verify` drops any value whose words don't all appear in the given text, and schools that are just the birth city. `degree_level` sets undergraduate/graduate/doctorate from the degree name, using word boundaries ("and philosophy" ≠ D.Phil); an Oxbridge MA and "First-Class Honours/Tripos" count as undergraduate.
  - Spouses and child counts fall back to regex (`spouses_from_text`, `children_from_text`) when the model leaves them empty. The model almost never fills them. The regex skips a "married" with a relative named before it. In a spot check, about 2 of 15 spouses were wrong.
  - Output: `laureate_details_raw.jsonl` (cache, one line per file stem, a later line wins) and `laureate_details.csv`, where the infobox wins and `*_source` = infobox/text. Education keeps both `education_infobox` and `education_text` + `undergrad_text`/`doctorate_text`.
- `chatbot/table_tool.py`:
  - The LLM never writes pandas code. It fills the pydantic form `TablePlan`, which holds up to 3 `TableQuery` items (actions: count / list / youngest / oldest / count_by_country / count_by_category).
  - `run_query` runs each query exactly. Counts use unique `nobel_id`s, so double winners count once.
  - `COUNTRY_ALIASES` maps US / UK / England…, and "United Kingdom" includes Scotland and Northern Ireland.
  - Ties in a ranking are written out explicitly.
- `chatbot/chat.py` builds a LangGraph `StateGraph`: `choose_tool` → `retrieve` and/or `run_table` → `build_prompt`.
  - `choose_tool` is one LLM call with `bind_tools([search_laureate_articles, query_nobel_table])` and `TOOL_PROMPT` (few-shot table examples). `query_nobel_table` subclasses `TablePlan`, so the tool's arguments are the form: choosing and planning happen in one call. The model may call both tools (route `"text+table"`, `BOTH_PROMPT`).
  - Fallbacks: no tool call → the `_TABLE_QUESTION` regex picks the route; invalid table arguments → `_fallback_plan` (`with_structured_output(TablePlan, method="json_schema")` + `PLANNER_PROMPT`); still no form → article search.
  - The article search uses the user's question, not the model's `query` (the model adds names from memory, e.g. "James Watson" for DNA, hiding Crick). Only pronoun follow-ups use the model's rewrite, if it contains a laureate name.
  - Thinking is always off (no toggle in the app): answers were better and faster with tools.
  - Answer prompts: `TEXT_PROMPT` (cite `[n]`), `TABLE_PROMPT` (repeat the exact numbers, mention Note/Tie lines), `BOTH_PROMPT`.
  - `prepare_turn(question, history, retriever)` returns a `Turn` with `route`, `sources` or `plan`/`table_result`.
  - `stream_answer(llm, turn)` yields `("thinking"|"answer", text)`.
  - Follow-ups: the previous user question is passed to the tool-choosing call ("Previous question: …"). Text route: a pronoun question with no laureate name searches with the model's rewrite, or else the previous question + the question.
  - The prompt keeps the last `HISTORY_TURNS` pairs.
- `chatbot/tracing.py`:
  - `prepare_turn` creates a `Trace` for every turn. The graph nodes call `trace.add(step, **data)` for these steps: choose_tool (the model's tool calls and arguments; choose_tool.fallback if none), retrieve, plan.raw / plan.fixed, table.query, `table.filter` (laureates left after each filter), table.result, prompt.
  - The caller must call `turn.trace.finish(answer)`. The app, `eval_chat.py` and `chat.py __main__` all do.
  - Output goes to `logs/chatbot.log` (readable, rotating) and `logs/turns.jsonl` (one record per turn). The app has a "Show debug log" toggle.
  - To debug a wrong table answer, compare `plan.raw` with `plan.fixed`, then check which `table.filter` drops the count.
- `fix_plan` also does two more things:
  - It forces `undergraduate_only` to match whether the question says undergrad/bachelor.
  - It moves a `birth_country` that isn't a known country into `birth_place`.
  - It turns `count_by_country` with a place filter into `count` unless the question compares countries.
- `birth_place` matches US states by their code (`"New York"` → any `", NY"` city), and anything else against `birth_city`/`birth_location`. An Oxford or Cambridge "MA" counts as an undergraduate degree.
- `app.py` uses `@st.cache_resource` for the retriever and the LLM, and keeps the messages (with sources and retrieval details) in `st.session_state.messages`.
- `eval_chat.py` runs regex-checked test cases through the same `prepare_turn`/`stream_answer` path and writes `eval_results.md`.
- **Speed:** about half of `qwen3.5:4b` runs on the CPU (3.9 GB loaded vs 4 GB of video memory). A cold first answer takes 1–2 min; warm answers take about 20–70 s.
- **Shared metadata:** both layers store `name`, `file_stem`, `url`, `prize_categories`/`prize_years` (joined with `;`), `first_prize_year`, `birth_year`, `death_year`, `birth_country`, and boolean `won_<category>` flags. Chroma rejects lists and None, so unknown values are left out.
- **Prize accuracy:** after the overrides, the prizes for all 1017 laureates match the official Nobel API. If you change the extraction regexes, re-check against the API (match on the `wikipedia.english` URL, falling back to surname). The API returns 403 unless the request sends a User-Agent header.

Environment: Windows / PowerShell 7.

## Python environment

Use the existing venv at `D:\python_evenv` (Python 3.12.3). Call it directly with `D:\python_evenv\Scripts\python.exe`, or activate it with `D:\python_evenv\Scripts\Activate.ps1`. It already has streamlit, langchain, langchain-ollama, langchain-chroma, langgraph, chromadb, pandas and anthropic. `langchain-anthropic` is not installed.

The planned app is a Streamlit + LangChain/LangGraph chatbot over this data, using local Ollama models: `qwen3.5:4b` for chat (it fits the 4 GB GTX 1650) and `nomic-embed-text` for embeddings (768 dimensions).

## Data schema

Every file has the same four top-level keys:

```json
{
  "url": "https://en.wikipedia.org/wiki/Aage_Bohr",
  "name": "Aage Bohr",
  "infobox": { "Born": "...", "Died": "...", "Awards": "...", "Fields": "...", ... },
  "content_sections": [ { "type": "h2" | "h3", "heading": "...", "text": "..." } ]
}
```

- **`infobox`** is a flat string-to-string map copied from the Wikipedia infobox. Its keys vary by person type: scientists have `Fields`/`Institutions`/`Alma mater`, politicians have `In office`/`Predecessor`, and so on. Values are raw scraped text, not structured data:
  - Dates are mixed into free text, e.g. `"( 1922-06-19 ) 19 June 1922 Copenhagen , Denmark"` and `"8 September 2009 (2009-09-08) (aged 87) Copenhagen , Denmark"`. To get dates, parse the ISO `YYYY-MM-DD` form; don't parse the prose.
  - List-like fields such as `Awards` and `Institutions` are joined with spaces, not with a delimiter.
  - The scrape adds spaces before punctuation (`Copenhagen , Denmark`), and footnote markers appear as `[ a ]` or `[ 9 ]`.
  - Laureates who are alive have `(age N)` in `Born` and no `Died` key.
  - Nobel prize category and year are not separate fields. Recover them from `Awards` (e.g. `"Nobel Prize in Physics (1975)"`) or from the intro text.
- **`content_sections`** is an ordered list of article sections. The first is usually `{"type": "h2", "heading": "Introduction", ...}`. The list is empty in about 108 files (for example `Aage_Bohr.json` and `14th_Dalai_Lama.json`), so code must handle infobox-only records.

## Known data quirks

- Some infobox keys contain non-breaking spaces (`Known\xa0for`). `load_record` normalizes them.
- The infobox has no single awards field (it can be `Awards`, `Notable awards`, …). Prize extraction therefore searches all infobox values joined together, which can pick up a nearby unrelated year. That's why there are override rows for Dawes, Snell, Mahfouz, Yalow and Bergström.
- `European_Union.json` has an infobox key that is an empty string (`""`). PowerShell's `ConvertFrom-Json` fails on it unless you pass `-AsHashtable`. Python's `json` module loads it fine.
- File sizes range from about 400 bytes (`W._B._Yeats.json`) to about 97 KB (`Kim_Dae-jung.json`), and infobox coverage is uneven across files.
