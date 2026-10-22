# How the chatbot finds information: two-layer vector embedding

This document explains how the laureate data is prepared so the chatbot can answer questions about it: what we do, why we do it, and how a question is answered.

---

## 1. The problem

We have 1017 Wikipedia articles about Nobel laureates, about **9.4 million characters** of text (roughly 2.3 million tokens).

The chat model (`qwen3.5:4b`, running locally through Ollama) can only read a few thousand tokens at a time. We can't give it all the articles and ask it a question. It's like asking someone to answer a question about one book in a library when they can only read three pages.

So before the model answers, we need to **find the few pages that matter** and give it only those. That's called *retrieval*, and the full pattern is *RAG (Retrieval-Augmented Generation)*:

```
question  ──►  find the relevant text  ──►  give that text + question to the LLM  ──►  answer
```

Vector embeddings are how we find the relevant text.

---

## 2. What an embedding is

An **embedding** turns a piece of text into a list of numbers (for our model, `nomic-embed-text`, **768 numbers**). Think of it as **a location on a map of meaning**:

- Texts about similar things land close together.
  *"He discovered the neutron"* and *"his work on subatomic particles"* end up near each other even though they share no words.
- Texts about different things land far apart.
  *"He discovered the neutron"* and *"she wrote poetry about rural Ireland"* end up far apart.

To search, we turn the **question** into a vector too and look for the stored texts whose vectors are **closest** to it. "Closest" is measured with **cosine similarity**, the angle between two vectors: a small angle means similar meaning.

This is better than keyword search because it matches **meaning, not exact words**.

A **vector database** (we use **ChromaDB**) stores all these vectors and finds the nearest ones quickly. It also stores **metadata** next to each vector (name, prize, year…), so a search can be limited to, for example, "only chunks about Marie Curie".

---

## 3. Why two layers?

Think of how a library works:

| Library | Our system |
|---|---|
| **Catalogue cards**: one card per book with title, author and subject. You use them to decide *which books* to open. | **Layer 1: profiles.** One short "ID card" per laureate. |
| **Pages inside the books**: where the actual details are. | **Layer 2: section chunks.** The article text, cut into small pieces. |

If we used only the pages (one layer), a search could return 5 random pages from 5 different books. They might mention the right words but come from the wrong person. Lots of pages also look alike ("He was born in… he studied at…").

With two layers, we first **pick the right laureates** using the catalogue cards, then **read the pages of those laureates only**. The answer is more focused and more accurate.

### Layer 1: profiles (`laureate_profiles`, 1017 entries)

One per laureate. It contains the name, the Nobel Prize, the most useful infobox fields (Known for, Fields, Institutions, Born, Died, Awards…) and the first ~1500 characters of the Introduction. A real example:

```
Name: Marie Curie
Nobel Prize: Physics (1903); Chemistry (1911)
Known for: Discovering polonium and radium (with Pierre) Researching radioactivity
Fields: Physics Chemistry
Institutions: University of Paris Institut du Radium ...
Born: Maria Salomea Skłodowska (1867-11-07) 7 November 1867 Warsaw, ...
Summary: Maria Salomea Skłodowska-Curie ... was a Polish and naturalised-French physicist and chemist ...
```

**Use:** answering *"who…?"* questions and working out which laureates a question is about.

### Layer 2: section chunks (`laureate_sections`, about 12,000 entries)

The full article text, split into pieces of about **1200 characters**. Every piece starts with **who it is about and which section it comes from**:

```
Abdus Salam — Introduction > Youth and education: Abdus Salam was born on 29 January 1926 in Jhang ...
```

That prefix matters. Without it, a chunk from an "Early life" section never mentions the person's name, so a search for *"Abdus Salam's childhood"* could miss it.

**Use:** finding the *details* that answer the question.

---

## 4. Preparing the data before embedding

Raw scraped text is messy, and messy text gives worse vectors. `chatbot/cleaning.py` does the following.

### Cleaning the text
- Removes footnote markers: `[ 9 ]`, `[ a ]`, `[ citation needed ]`.
- Fixes the scraper's spacing: `Copenhagen , Denmark` → `Copenhagen, Denmark`.
- Replaces invisible characters such as non-breaking spaces (`Known\xa0for` → `Known for`).

### Finding each laureate's Nobel Prize
The files have no "prize" field, so we find phrases like *"Nobel Prize in Physics (1975)"* in the text. We look in this order:
1. **Infobox** award lists: the cleanest source, and they can list several prizes (*"Nobel Prize in Chemistry (1958, 1980)"*).
2. **Introduction**: only the *first* mention, because later mentions are often about someone else. For example, Alva Myrdal's intro also mentions her husband's Economics prize.
3. The **next few sections**: also first mention only.

**Hand-checked corrections** are stored in `prize_overrides.csv`. Every row links to the official **Nobel Foundation API** (`api.nobelprize.org`). This file covers three groups:
- 29 laureates whose file never mentions their prize (e.g. Nelson Mandela, the European Union)
- 10 whose file gives the prize but no year
- 5 where the text extraction picked up a nearby, unrelated date

**Accuracy check:** we compared every automatically extracted prize with the official Nobel API. After the corrections, **all 1017 laureates have the correct category and year(s)**, including double winners such as Marie Curie, Linus Pauling, John Bardeen, Frederick Sanger, K. Barry Sharpless, the Red Cross (3 prizes) and UNHCR (2 prizes).

### Other facts
Birth year and death year (taken from the `YYYY-MM-DD` dates) and a best-effort birthplace and country.

---

## 5. How the text is cut into chunks

| Setting | Value | Why |
|---|---|---|
| Chunk size | ~1200 characters | Big enough to hold a complete idea, small enough to be about *one* topic. A chunk covering three topics gets a "blurry" vector that matches nothing well. |
| Overlap | 150 characters | Neighbouring chunks share a little text, so a sentence cut at a boundary still appears whole in one of them. |
| Cut points | paragraph → sentence → word | The splitter tries to cut at natural breaks, never mid-word. |
| Short sections | merged if < 300 characters | A 100-character chunk carries too little meaning. It's glued onto the next section. |
| Sub-headings | `Career > Bell Labs` | Sub-sections (h3) keep their parent heading, so the chunk still says where in the life story it belongs. |

---

## 6. How a question is answered

The retrieval step is in `chatbot/retriever.py`. It works like this:

```
                         ┌──────────────────────────┐
     user question  ───► │ Does it name a laureate? │
                         └───────────┬──────────────┘
                     yes             │             no
            ┌────────────────────────┴───────────────────────┐
            ▼                                                 ▼
  Route A: search layer 2                    Route B: search layer 1 (profiles)
  ONLY that laureate's chunks                → top 3–5 most relevant laureates
                                             → search layer 2 ONLY their chunks
            │                                                 │
            └──────────────────────┬──────────────────────────┘
                                   ▼
              + safety net: a small search over ALL chunks
                (catches details no profile mentions)
                                   ▼
            best chunks + question  ──►  LLM  ──►  answer with sources
```

**Examples**
- *"What did Abdus Salam work on?"* The question names a laureate, so it takes **Route A**: only Salam's chunks are searched.
- *"Who discovered the structure of DNA?"* No name, so it takes **Route B**. The profiles point to Watson, Crick and Wilkins, and then their chunks are searched for the details.
- *"Which laureate was imprisoned for their activism?"* No profile may mention prison, so the **safety net** search over all chunks finds the relevant passages.

### How a name is recognised

The retriever builds a list of names from layer 1:
- **Full names**, written with or without accents: *"Lech Walesa"* finds *Lech Wałęsa*.
- **First + last name**: *"George Snell"* finds *George Davis Snell*.
- **Surnames on their own**:
  - *"Einstein"* works.
  - *"Bohr"* finds both Niels and Aage Bohr.
  - A surname shared by more than 4 laureates (*"Smith"*) is too vague and is ignored.
- **Short names for organisations**: *UNHCR*, *Red Cross*, *IAEA*, *Dalai Lama*…

Some surnames are also ordinary words: *Born*, *King*, *Hill*. These only count when written with a capital letter, so *"Where was Max Born born?"* finds Max Born once, not twice.

### Extra rules that keep the results useful

| Rule | Why |
|---|---|
| A named laureate's **profile** is always included | It holds the short facts (prize, year, birth) even when the article text is empty, as it is for Einstein and Niels Bohr. |
| In Route A, the chunk budget is **split between the people** named | *"Compare Bohr and Einstein"* gets text about both, not just one. |
| In Route B, at most **3 chunks per laureate** | Otherwise Crick's long DNA section fills every slot and pushes out Wilkins and Watson. |
| Route B uses a **prize filter** when the question names one (*"Peace Prize laureates who…"*) | Only Peace laureates' profiles are searched. *"The physics of black holes"* doesn't trigger it, because it's about the subject, not the prize. |
| In Route A, safety-net chunks are kept **only if they mention the named person** | Otherwise *"Where was Max Born born?"* also returns the early life of Max Perutz. |
| The text handed to the model is capped at **~9000 characters** (~2300 tokens) | A small local model has a small context window. Profiles come first (cut to their first ~800 characters), then chunks from closest to farthest. |

Each piece of text is numbered `[1]`, `[2]`… with its source (*"Abdus Salam — Government work"*), so the chat model can cite where an answer came from.

### From sources to an answer (`chatbot/chat.py`)

The chat model gets three things:
- **Short rules:** answer only from the sources, cite them as `[n]`, and say so when the answer isn't there.
- **The numbered sources.**
- **The last 2 question/answer pairs**, so it can follow the conversation.

Follow-up questions like *"When did **she** die?"* name nobody. When a question has a pronoun but no name, the previous question is added to the search text, *"Tell me about Marie Curie. When did she die?"*, so the retriever still finds Marie Curie.

**Known limit:** a capitalised ordinary word that is also a surname still counts as a name. *"Who was the King of Sweden?"* is routed to Martin Luther King Jr. The safety net still searches all chunks, so the right text can still be found.

---

## 7. Technical details

- **Task prefixes.** `nomic-embed-text` was trained to see `search_document: ` before stored text and `search_query: ` before questions. `chatbot/embeddings.py` adds these automatically. Ingestion and search **must** use the same class, or the vectors won't be comparable.
- **Cosine distance.** Chroma measures straight-line (L2) distance by default. We set cosine, which works better for text embeddings.
- **Metadata for filtering.** Each entry stores `name`, `url`, `prize_categories`, `prize_years`, `first_prize_year`, `birth_year`, `death_year`, `birth_country`, and a flag for each prize won (`won_physics`, `won_peace`, …). Chroma metadata can't hold lists, so several prizes are joined with `;` (`"Physics;Chemistry"`). The `won_…` flags make filtering easy, e.g. `{"won_physics": True}`.
- **Stable IDs.** Profiles are `Aage_Bohr::profile` and chunks are `Abdus_Salam::s3::c1` (section 3, chunk 1).
- **Full rebuild.** Every ingestion run deletes and rebuilds both collections, so the database always matches the current code.

---

## 8. What embeddings can't do

Embeddings find *similar text*. They can't **count, sort or compare numbers**. Questions such as:
- *"How many Chemistry laureates were born in Germany?"*
- *"Who was the youngest Physics laureate?"*

can't be answered by searching for similar paragraphs. The search only ever returns about 10 pieces of text, so the model can't see all 1000 laureates to count them.

### The counting tool

These questions go to a **table** instead, one row per prize:
- **Built by** `chatbot/build_facts.py`.
- **Main source:** the **official Nobel Prize API**. It covers all 1028 laureates, with exact birth dates, birth country (today's name), gender and the date each prize was awarded.
- **Universities:** from the Wikipedia infobox *Education / Alma mater* fields. A school counts as **undergraduate** only when a bachelor's degree is written next to it, e.g. *"Harvard University (AB)"*.

The model is given **two tools** and decides itself which one to call (this is called *tool calling*):

```
question ─► the model looks at the question and calls a tool
              │                                         │
   search_laureate_articles                     query_nobel_table
   ("What did Salam work on?")                  ("How many were born in Spain?")
              ▼                                         ▼
     article search (sections 3–6)      the tool's arguments are a small FORM:
                                          action = count, birth_country = Spain
                                                  ▼
                                        pandas runs the form on the table → "Count: 7, ..."
              └──────────────┬──────────────────────────┘
                             ▼
             the model writes the answer from those exact facts
```

Choosing the tool and filling in the form happen in **one** model call. If a question needs both (*"How many Spanish laureates are there, and what did Ramón y Cajal discover?"*), the model can call both tools. **Thinking is turned off**: with tools the answers were better and much faster.

There are safety nets for a small model's slips:
- If the model calls no tool, a keyword rule decides ("how many", "youngest"…).
- If it fills the form wrongly, a second call fills it with a strict format, and code checks the form against the question (`fix_plan`).
- The article search uses **your own words**, not the model's search text. The model sometimes adds a name from memory (*"James Watson"* for a DNA question), which would hide Crick and Wilkins.

Why a form instead of letting the model write code? A 4B model writes wrong pandas code too often. Filling in *"action = youngest, rank = 3"* is easy for it, and the counting itself is done by normal code, so **the numbers are always exact**. The app's *Table lookup* panel shows the form and the raw result, so you can check every number.

This "choose a tool" step is built with **LangGraph** (`chatbot/chat.py`): `choose_tool → retrieve and/or run_table → build_prompt`.

**Limits of the table:**
- **Countries** use **today's borders**. Someone born in Breslau (German Empire, 1884) counts as born in Poland. The country at birth is kept too (`birth_country_then`).
- **University data** comes from Wikipedia infoboxes and is incomplete. Many laureates list schools without degrees, so *"undergraduate at Harvard"* is a lower bound. The bot says so.
- **Nationality** isn't in the table, so "from Spain" means **born in Spain**.
- **Places smaller than a country** work for US states (*"born in New York"* means the whole state) and for city names.

### Finding out why an answer is wrong: the debug log

Every answer is logged step by step in `logs/chatbot.log` and `logs/turns.jsonl`. To see the log, turn on **Show debug log** in the app, or run `python -m chatbot.tracing`. For a counting question it shows:

```
choose_tool   query_nobel_table {"queries": [{"action": "count", "university": "Oxford"}]}   ← the model's tool call
plan.raw      {"action": "count", "university": "Oxford"}                    ← what the model filled in
plan.fixed    {"action": "count", "university": "Oxford", "undergraduate_only": true}   ← after the code's checks
table.filter  people (organisations excluded)  → 1000 laureates left
table.filter  undergraduate degree at Oxford   → 16 laureates left
answer        "There are 16 Nobel laureates..."
```

This makes it easy to see **which step** went wrong:
- the model filled in the form badly (`plan.raw`)
- a filter removed too many laureates (`table.filter`)
- the data simply doesn't contain the fact
- the final answer misreported a correct result

---

## 9. Files and commands

| File | Role |
|---|---|
| `chatbot/config.py` | All settings: paths, model names, chunk sizes |
| `chatbot/cleaning.py` | Text cleaning and prize/birth extraction |
| `chatbot/embeddings.py` | `nomic-embed-text` with the search prefixes |
| `chatbot/ingest.py` | Builds both layers in Chroma and writes `laureates.csv` |
| `chatbot/retriever.py` | Answers "which text is relevant?": name routing, Route A/B, safety net |
| `chatbot/llm.py` | The chat model (`qwen3.5:4b` through Ollama) |
| `chatbot/chat.py` | One chat turn (LangGraph): choose article search or the table → prompt → streamed answer |
| `chatbot/build_facts.py` | Builds `laureate_prizes.csv` from the official Nobel API (+ Wikipedia education) |
| `chatbot/table_tool.py` | The counting tool: runs the model's filled-in form with pandas |
| `nobel_api_laureates.json` | Saved download from api.nobelprize.org (generated) |
| `laureate_prizes.csv` | The counting table, one row per prize (generated) |
| `app.py` | The Streamlit chat app |
| `eval_chat.py` | 33 test questions; writes `eval_results.md` |
| `prize_overrides.csv` | Hand-checked prizes from the official Nobel API |
| `chroma_db/` | The vector database (generated; don't edit) |
| `laureates.csv` | Structured table (generated) |

```powershell
# Check prize extraction without building anything
D:\python_evenv\Scripts\python.exe -m chatbot.cleaning

# Build / rebuild the vector database (Ollama must be running; takes several minutes)
D:\python_evenv\Scripts\python.exe -m chatbot.ingest

# See what the retriever finds (demo questions, or your own)
D:\python_evenv\Scripts\python.exe -m chatbot.retriever
D:\python_evenv\Scripts\python.exe -m chatbot.retriever "What did Abdus Salam work on?"

# Rebuild the counting table (add --refresh to download the Nobel data again)
D:\python_evenv\Scripts\python.exe -m chatbot.build_facts

# Start the chat app (opens http://localhost:8501)
D:\python_evenv\Scripts\streamlit.exe run app.py

# Run the test questions (writes eval_results.md)
D:\python_evenv\Scripts\python.exe eval_chat.py
```
