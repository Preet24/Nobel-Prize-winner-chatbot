"""
Central settings for the Nobel laureate chatbot.

Every other module imports its paths, model names and chunk sizes from here,
so an experiment (e.g. a different chunk size) only needs one change.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# Project root = the folder that contains this "chatbot" package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Raw input: one Wikipedia-scraped JSON file per laureate.
DATA_DIR = PROJECT_ROOT / "laureates_data"

# Hand-checked prize facts for laureates whose JSON file never mentions
# their Nobel Prize (filled in from nobelprize.org).
PRIZE_OVERRIDES_CSV = PROJECT_ROOT / "prize_overrides.csv"

# Output: the Chroma vector database is saved here (created by ingest.py).
CHROMA_DIR = PROJECT_ROOT / "chroma_db"

# Output: a plain table of laureate facts (prize, year, birth...) for
# counting/filtering questions that embeddings can't answer.
LAUREATES_CSV = PROJECT_ROOT / "laureates.csv"

# Official Nobel Prize API data, downloaded once by build_facts.py, and the
# table built from it (one row per prize) that the counting tool queries.
NOBEL_API_CACHE = PROJECT_ROOT / "nobel_api_laureates.json"
PRIZES_CSV = PROJECT_ROOT / "laureate_prizes.csv"

# Biographical details (education, birthplace, citizenship, parents...) read
# from the article text by extract_details.py: the model's raw answers
# (one JSON line per laureate, so a stopped run can resume) and the final
# table that combines them with the infobox.
DETAILS_CACHE = PROJECT_ROOT / "laureate_details_raw.jsonl"
DETAILS_CSV = PROJECT_ROOT / "laureate_details.csv"

# Logs of every chat turn (chatbot.log + turns.jsonl), see tracing.py.
LOG_DIR = PROJECT_ROOT / "logs"

# ---------------------------------------------------------------------------
# Ollama models
# ---------------------------------------------------------------------------
EMBED_MODEL = "nomic-embed-text"  # 768-dimension embedding model
CHAT_MODEL = "qwen3.5:4b"         # fits the 4 GB GTX 1650
CHAT_NUM_CTX = 8192      # context window given to the chat model (tokens);
                         # retrieved text ~2300 + history + answer fit easily
CHAT_TEMPERATURE = 0.3   # low = sticks to the retrieved facts, less invention
HISTORY_TURNS = 2        # how many earlier question/answer pairs the model sees

# ---------------------------------------------------------------------------
# Chroma collections (the two "layers")
# ---------------------------------------------------------------------------
PROFILE_COLLECTION = "laureate_profiles"  # layer 1: one summary per laureate
SECTION_COLLECTION = "laureate_sections"  # layer 2: detailed text chunks

# ---------------------------------------------------------------------------
# Chunking settings (sizes are in characters; roughly 4 characters = 1 token)
# ---------------------------------------------------------------------------
CHUNK_SIZE = 1200        # target size of one layer-2 chunk
CHUNK_OVERLAP = 150      # characters repeated between neighbouring chunks, so a
                         # sentence cut at a boundary still appears whole once
MIN_SECTION_CHARS = 300  # sections shorter than this are merged with the next
                         # one; tiny chunks embed poorly and waste search slots

PROFILE_INTRO_CHARS = 1500  # how much of the Introduction goes into a profile
PROFILE_FIELD_CHARS = 300   # cap for each infobox value in a profile
                            # (some "Awards" lists are very long)

# How many documents are sent to Ollama in one embedding request.
EMBED_BATCH_SIZE = 64

# ---------------------------------------------------------------------------
# Retrieval settings (used by retriever.py)
# ---------------------------------------------------------------------------
PROFILE_K = 4      # Route B: how many laureates layer 1 picks
SECTION_K = 6      # how many layer-2 chunks to take for the chosen laureates
SAFETY_NET_K = 3   # extra layer-2 chunks from an unfiltered search
MAX_CHUNKS_EACH = 3  # Route B: at most this many chunks per laureate, so
                     # one long article can't fill every slot
MAX_NAMED = 4      # a surname shared by more laureates than this ("Smith")
                   # is too vague to route on, so it is ignored

# How much retrieved text is handed to the chat model. ~4 characters = 1
# token, so 9000 characters is ~2300 tokens, leaving room for the question,
# instructions and answer inside a small local model's context window.
MAX_CONTEXT_CHARS = 9000
PROFILE_CONTEXT_CHARS = 800  # profiles are cut to their first lines (name,
                             # prize, known for, born...) in the context
