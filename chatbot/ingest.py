"""
Build the two-layer vector store from laureates_data/*.json.

  Layer 1  "laureate_profiles" : ONE short profile per laureate
           (name, prize, key infobox facts, start of the Introduction).
           Used to find WHICH laureates a question is about.

  Layer 2  "laureate_sections" : MANY ~1200-character chunks of article text,
           each starting with "<Name> — <Section heading>: ".
           Used to find the DETAILS that answer the question.

It also writes laureates.csv (one row per laureate) for questions that need
counting or filtering rather than text search.

Run (takes several minutes; Ollama must be running):
    D:\\python_evenv\\Scripts\\python.exe -m chatbot.ingest

Every run deletes and rebuilds both collections, so the database always
matches the current cleaning/chunking code.
"""

import csv

import chromadb
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from tqdm import tqdm

from chatbot.cleaning import (
    extract_life_facts,
    extract_prizes,
    load_prize_overrides,
    load_record,
)
from chatbot.config import (
    CHROMA_DIR,
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    DATA_DIR,
    EMBED_BATCH_SIZE,
    LAUREATES_CSV,
    MIN_SECTION_CHARS,
    PROFILE_COLLECTION,
    PROFILE_FIELD_CHARS,
    PROFILE_INTRO_CHARS,
    SECTION_COLLECTION,
)
from chatbot.embeddings import NomicEmbeddings

# Infobox fields that say the most about a laureate. They go first in the
# profile; other fields follow. Fields in SKIP_FIELDS add only noise.
PREFERRED_FIELDS = [
    "Known for", "Fields", "Occupation", "Occupations", "Institutions",
    "Alma mater", "Education", "Nationality", "Citizenship", "Born", "Died",
    "Notable works", "Awards", "Notable awards", "Purpose", "Founded",
    "Headquarters",
]
SKIP_FIELDS = {"Name", "Signature", "Website", "Coordinates", "Image", "Seal"}
MAX_INFOBOX_CHARS = 1500  # total budget for infobox text inside one profile


# ===========================================================================
# Metadata shared by every document of one laureate
# ===========================================================================
def build_metadata(record: dict, prizes: dict[str, list[int]], facts: dict) -> dict:
    """
    Chroma metadata values must be single str/int/float/bool values (no
    lists, no None). So:
      * several prizes are joined with ";"  ("Physics;Chemistry")
      * each won category also gets a True flag (won_physics=True), which
        makes filters easy: {"won_physics": True}
      * unknown values (e.g. no birth year) are simply left out
    """
    years = sorted(y for ys in prizes.values() for y in ys)
    meta = {
        "name": record["name"],
        "file_stem": record["file_stem"],
        "url": record["url"],
        "prize_categories": ";".join(prizes),
        "prize_years": ";".join(str(y) for y in years),
    }
    if years:
        meta["first_prize_year"] = years[0]
    for category in prizes:
        flag = "won_" + category.lower().replace(" or ", "_").replace(" ", "_")
        meta[flag] = True  # e.g. won_physiology_medicine, won_economic_sciences
    for key in ("birth_year", "death_year"):
        if facts[key] is not None:
            meta[key] = facts[key]
    if facts["birth_country"]:
        meta["birth_country"] = facts["birth_country"]
    return meta


def prize_summary(prizes: dict[str, list[int]]) -> str:
    """{'Physics': [1903], 'Chemistry': [1911]} -> 'Physics (1903); Chemistry (1911)'"""
    return "; ".join(
        f"{cat} ({', '.join(map(str, ys))})" if ys else cat
        for cat, ys in prizes.items()
    )


# ===========================================================================
# Layer 1: one profile per laureate
# ===========================================================================
def build_profile(record: dict, prizes: dict, meta: dict) -> Document:
    """
    A compact "ID card" of the laureate. Example text:

        Name: Aage Bohr
        Nobel Prize: Physics (1975)
        Known for: Geometry of atomic nuclei
        Fields: Nuclear physics
        ...
        Summary: <first 1500 characters of the Introduction>
    """
    infobox = record["infobox"]
    lines = [f"Name: {record['name']}", f"Nobel Prize: {prize_summary(prizes)}"]

    # Preferred fields first, then the rest, each capped in length, until the
    # infobox budget is used up.
    ordered = [k for k in PREFERRED_FIELDS if k in infobox]
    ordered += [k for k in infobox if k not in ordered and k not in SKIP_FIELDS]
    used = 0
    for key in ordered:
        value = infobox[key][:PROFILE_FIELD_CHARS]
        if not value or used >= MAX_INFOBOX_CHARS:
            continue
        lines.append(f"{key}: {value}")
        used += len(value)

    sections = record["content_sections"]
    if sections:
        lines.append("Summary: " + sections[0]["text"][:PROFILE_INTRO_CHARS])

    return Document(
        page_content="\n".join(lines),
        metadata={**meta, "layer": "profile"},
    )


# ===========================================================================
# Layer 2: section chunks
# ===========================================================================
SPLITTER = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE,
    chunk_overlap=CHUNK_OVERLAP,
    # Try to cut at paragraph breaks first, then sentences, then words.
    separators=["\n\n", "\n", ". ", " ", ""],
)


def merge_sections(record: dict) -> list[tuple[str, str]]:
    """
    Turn content_sections into (heading, text) pairs ready for splitting.

    * h3 headings get their parent h2 heading: "Career > Bell Labs", so a
      chunk still says which part of the life story it comes from.
    * sections shorter than MIN_SECTION_CHARS are glued onto the following
      section, because a 100-character chunk carries too little meaning.
    """
    merged: list[tuple[str, str]] = []
    parent_h2 = ""
    pending_heading, pending_text = "", ""

    for section in record["content_sections"]:
        if not section["text"]:
            continue
        if section["type"] == "h2":
            parent_h2 = section["heading"]
            heading = section["heading"]
        else:
            heading = f"{parent_h2} > {section['heading']}" if parent_h2 else section["heading"]

        if pending_text:
            # Glue the short previous section in front of this one.
            heading = f"{pending_heading} / {heading}"
            text = f"{pending_text}\n\n{section['text']}"
        else:
            text = section["text"]

        if len(text) < MIN_SECTION_CHARS:
            pending_heading, pending_text = heading, text  # keep collecting
        else:
            merged.append((heading, text))
            pending_heading, pending_text = "", ""

    if pending_text:  # a short section left over at the very end
        merged.append((pending_heading, pending_text))
    return merged


def build_section_chunks(record: dict, meta: dict) -> list[Document]:
    """
    Split each (merged) section into ~CHUNK_SIZE pieces. Every chunk starts
    with "<Name> — <Heading>: " because a chunk from an "Early life" section
    otherwise never says WHO it is about, and search would miss it.
    """
    docs = []
    for section_no, (heading, text) in enumerate(merge_sections(record)):
        for chunk_no, piece in enumerate(SPLITTER.split_text(text)):
            docs.append(
                Document(
                    page_content=f"{record['name']} — {heading}: {piece}",
                    metadata={
                        **meta,
                        "layer": "section",
                        "heading": heading,
                        # Stable ID parts, also handy for showing sources.
                        "section_no": section_no,
                        "chunk_no": chunk_no,
                    },
                )
            )
    return docs


# ===========================================================================
# Writing to Chroma
# ===========================================================================
def open_fresh_collection(client, name: str, embeddings) -> Chroma:
    """Delete the collection if it exists, then create it empty."""
    try:
        client.delete_collection(name)
    except Exception:
        pass  # did not exist yet
    return Chroma(
        client=client,
        collection_name=name,
        embedding_function=embeddings,
        # Compare vectors by cosine similarity (angle between vectors).
        # Chroma's default is L2 distance; cosine suits text embeddings better.
        collection_metadata={"hnsw:space": "cosine"},
    )


def add_in_batches(store: Chroma, docs: list[Document], ids: list[str], label: str):
    """Send documents to Ollama/Chroma in batches, with a progress bar."""
    for start in tqdm(range(0, len(docs), EMBED_BATCH_SIZE), desc=label, unit="batch"):
        end = start + EMBED_BATCH_SIZE
        store.add_documents(docs[start:end], ids=ids[start:end])


def write_csv(rows: list[dict]):
    """laureates.csv: the structured table for counting/filtering questions."""
    columns = [
        "name", "file_stem", "url", "prize_categories", "prize_years",
        "first_prize_year", "prize_source", "birth_year", "death_year",
        "birth_place", "birth_country", "n_sections",
    ]
    with open(LAUREATES_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


# ===========================================================================
# Main
# ===========================================================================
def main():
    overrides = load_prize_overrides()
    profiles, profile_ids = [], []
    chunks, chunk_ids = [], []
    csv_rows = []

    # --- Step 1: read, clean and turn every file into documents -----------
    for path in sorted(DATA_DIR.glob("*.json")):
        record = load_record(path)

        # Hand-checked prizes (from nobelprize.org) win over regex extraction.
        if path.stem in overrides:
            prizes, prize_source = overrides[path.stem], "override"
        else:
            prizes, prize_source = extract_prizes(record)

        facts = extract_life_facts(record)
        meta = build_metadata(record, prizes, facts)

        profiles.append(build_profile(record, prizes, meta))
        profile_ids.append(f"{path.stem}::profile")

        for doc in build_section_chunks(record, meta):
            chunks.append(doc)
            chunk_ids.append(
                f"{path.stem}::s{doc.metadata['section_no']}::c{doc.metadata['chunk_no']}"
            )

        csv_rows.append({
            "name": record["name"],
            "file_stem": path.stem,
            "url": record["url"],
            "prize_categories": meta["prize_categories"],
            "prize_years": meta["prize_years"],
            "first_prize_year": meta.get("first_prize_year", ""),
            "prize_source": prize_source,
            "birth_year": facts["birth_year"] or "",
            "death_year": facts["death_year"] or "",
            "birth_place": facts["birth_place"],
            "birth_country": facts["birth_country"],
            "n_sections": len(record["content_sections"]),
        })

    print(f"Prepared {len(profiles)} profiles and {len(chunks)} section chunks.")

    # --- Step 2: the structured table --------------------------------------
    write_csv(csv_rows)
    print(f"Wrote {LAUREATES_CSV.name}")

    # --- Step 3: embed and store both layers --------------------------------
    embeddings = NomicEmbeddings()
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))

    profile_store = open_fresh_collection(client, PROFILE_COLLECTION, embeddings)
    add_in_batches(profile_store, profiles, profile_ids, "Layer 1 profiles")

    section_store = open_fresh_collection(client, SECTION_COLLECTION, embeddings)
    add_in_batches(section_store, chunks, chunk_ids, "Layer 2 sections")

    print(f"Done. Vector store saved in {CHROMA_DIR}")


if __name__ == "__main__":
    main()
