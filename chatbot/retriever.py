"""
Find the text that answers a question, using the two layers built by ingest.py.

    question ──► does it name a laureate?
                   yes → Route A: search layer 2, only that laureate's chunks
                   no  → Route B: search layer 1 (profiles) for the best
                                  laureates, then layer 2, only their chunks
             ──► + safety net: a small unfiltered search over ALL chunks
             ──► merged, de-duplicated documents (+ a ready-made context
                 string for the chat model, with numbered sources)

Try it (Ollama must be running):
    D:\\python_evenv\\Scripts\\python.exe -m chatbot.retriever
    D:\\python_evenv\\Scripts\\python.exe -m chatbot.retriever "What did Abdus Salam work on?"
"""

import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

import chromadb
from langchain_chroma import Chroma
from langchain_core.documents import Document

from chatbot.config import (
    CHROMA_DIR,
    MAX_CHUNKS_EACH,
    MAX_CONTEXT_CHARS,
    MAX_NAMED,
    PROFILE_COLLECTION,
    PROFILE_CONTEXT_CHARS,
    PROFILE_K,
    SAFETY_NET_K,
    SECTION_COLLECTION,
    SECTION_K,
)
from chatbot.embeddings import NomicEmbeddings

# ===========================================================================
# Text normalisation for name matching
# ===========================================================================
# Letters that Unicode does not split into "base letter + accent", so the
# accent-stripping below would miss them.
_EXTRA_LETTERS = str.maketrans({
    "ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D",
    "ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ı": "i",
})


def fold(text: str) -> str:
    """Remove accents but keep case: 'Wałęsa' -> 'Walesa', 'Schrödinger' -> 'Schrodinger'."""
    text = unicodedata.normalize("NFKD", text.translate(_EXTRA_LETTERS))
    return "".join(c for c in text if not unicodedata.combining(c))


def tokenize(text: str) -> list[str]:
    """Accent-free words, case kept. Punctuation splits words: "Einstein's" -> ['Einstein', 's']."""
    return re.findall(r"[^\W_]+", fold(text))


# Short names people use for organisations (the official names are long).
ALIASES = {
    "UNHCR": "United Nations High Commissioner for Refugees",
    "ICRC": "International Committee of the Red Cross",
    "Red Cross": "International Committee of the Red Cross",
    "IFRC": "International Federation of Red Cross and Red Crescent Societies",
    "IAEA": "International Atomic Energy Agency",
    "OPCW": "Organisation for the Prohibition of Chemical Weapons",
    "IPCC": "Intergovernmental Panel on Climate Change",
    "ICAN": "International Campaign to Abolish Nuclear Weapons",
    "ICBL": "International Campaign to Ban Landmines",
    "ILO": "International Labour Organization",
    "IPPNW": "International Physicians for the Prevention of Nuclear War",
    "MSF": "Médecins Sans Frontières",
    "Doctors Without Borders": "Médecins Sans Frontières",
    "WFP": "World Food Programme",
    "UN": "United Nations",
    "EU": "European Union",
    "Dalai Lama": "14th Dalai Lama",
    "Martin Luther King": "Martin Luther King Jr.",
}

# Parts of a name that are not used for matching: "(chemist)", "Jr.", "III".
_NAME_NOISE = re.compile(r"\([^)]*\)|,?\s+(?:Jr|Sr)\.?$|\s+(?:II|III|IV)$")


# ===========================================================================
# Recognising laureate names in a question
# ===========================================================================
class NameIndex:
    """
    Built once from the metadata of layer 1. Knows two kinds of keys:

    * phrases: a laureate's full name ("abdus salam"), the first + last name
      ("george snell" for George Davis Snell) and the ALIASES above.
    * surnames: the last word of each PERSON's name ("salam" -> Abdus Salam).
      A surname can point to several laureates ("bohr" -> Niels and Aage).

    Organisations have no birth year in the metadata; they get no surname key,
    otherwise "Cross" would match the Red Cross.
    """

    def __init__(self, metadatas: list[dict], profile_texts: list[str]):
        self.phrases: dict[tuple[str, ...], set[str]] = {}
        self.surnames: dict[str, set[str]] = {}
        self.stems: dict[str, str] = {}  # name -> file_stem

        for meta in metadatas:
            name = meta["name"]
            self.stems[name] = meta["file_stem"]
            words = [w.lower() for w in tokenize(_NAME_NOISE.sub("", name))]
            if not words:
                continue
            self._add_phrase(words, name)
            is_person = "birth_year" in meta
            if is_person and len(words) >= 3:
                self._add_phrase([words[0], words[-1]], name)
            if is_person and len(words) >= 2 and len(words[-1]) >= 3:
                self.surnames.setdefault(words[-1], set()).add(name)

        for alias, name in ALIASES.items():
            if name in self.stems:
                self._add_phrase([w.lower() for w in tokenize(alias)], name)

        self.longest = max(len(p) for p in self.phrases)

        # Some surnames are also ordinary English words ("Born", "King",
        # "Hill", "Memorial"). We detect them by how often they appear in
        # lower case in the profiles, and for those words require a capital
        # letter in the question: "Max Born" or "Born" yes, "born" no.
        lower_counts = Counter(w for text in profile_texts for w in tokenize(text) if w.islower())
        self.common_words = {w for w in lower_counts if lower_counts[w] >= 2}

    def _add_phrase(self, words: list[str], name: str):
        self.phrases.setdefault(tuple(words), set()).add(name)

    def find(self, question: str) -> list[str]:
        """Return the laureate names mentioned in the question (in order of appearance)."""
        tokens = tokenize(question)
        lower = [t.lower() for t in tokens]
        found: list[str] = []
        i = 0
        while i < len(tokens):
            matched = False
            # 1) Longest phrase first, so "marie curie" wins over the surname "curie".
            for length in range(min(self.longest, len(tokens) - i), 0, -1):
                key = tuple(lower[i:i + length])
                names = self.phrases.get(key)
                if not names:
                    continue
                # A one-word phrase ("UNICEF", "Memorial") that is also an
                # ordinary word only counts when written with a capital.
                if length == 1 and key[0] in self.common_words and not tokens[i][0].isupper():
                    continue
                found += [n for n in sorted(names) if n not in found]
                i += length
                matched = True
                break
            if matched:
                continue
            # 2) A surname on its own: "Einstein", "Bohr".
            word = lower[i]
            names = self.surnames.get(word, set())
            if 0 < len(names) <= MAX_NAMED and (word not in self.common_words or tokens[i][0].isupper()):
                found += [n for n in sorted(names) if n not in found]
            i += 1
        return found


# ===========================================================================
# Recognising a prize category ("Physics laureate", "Nobel Peace Prize")
# ===========================================================================
CATEGORY_FLAGS = {
    "Physics": ("physics", "won_physics"),
    "Chemistry": ("chemistry", "won_chemistry"),
    "Physiology or Medicine": ("medicine|physiology", "won_physiology_medicine"),
    "Literature": ("literature", "won_literature"),
    "Peace": ("peace", "won_peace"),
    "Economic Sciences": (r"econom\w*", "won_economic_sciences"),
}
_PRIZE_WORDS = r"(?:nobel|prize|prizes|laureate|laureates|winner|winners|won|awarded)"


def find_category(question: str) -> str | None:
    """
    The category, but only when the question talks about the PRIZE
    ("Peace Prize winners", "laureate in physics"), not just the subject
    ("the physics of black holes"). Only returned if exactly one matches.
    """
    q = question.lower()
    hits = []
    for category, (pattern, _) in CATEGORY_FLAGS.items():
        before = rf"\b(?:{pattern})\b\W+(?:\w+\W+){{0,2}}?{_PRIZE_WORDS}\b"
        after = rf"\b{_PRIZE_WORDS}\W+(?:\w+\W+){{0,3}}?(?:{pattern})\b"
        if re.search(before, q) or re.search(after, q):
            hits.append(category)
    return hits[0] if len(hits) == 1 else None


# ===========================================================================
# The retriever
# ===========================================================================
@dataclass
class RetrievalResult:
    question: str
    route: str                                       # "A" (named) or "B" (profile search)
    laureates: list[str]                             # laureates whose chunks were searched
    category: str | None = None                      # prize filter used in Route B, if any
    docs: list[Document] = field(default_factory=list)  # profiles first, then chunks by relevance


class LaureateRetriever:
    """Load the two Chroma collections once, then call .retrieve(question)."""

    def __init__(self):
        embeddings = NomicEmbeddings()  # same class as ingestion, or vectors don't match
        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        self.profiles = Chroma(client=client, collection_name=PROFILE_COLLECTION,
                               embedding_function=embeddings)
        self.sections = Chroma(client=client, collection_name=SECTION_COLLECTION,
                               embedding_function=embeddings)

        everything = self.profiles.get(include=["metadatas", "documents"])
        if not everything["ids"]:
            raise RuntimeError("The vector store is empty. Run: python -m chatbot.ingest")
        self.names = NameIndex(everything["metadatas"], everything["documents"])

    # ---- small helpers ----------------------------------------------------
    @staticmethod
    def _tag(results, found_by: str) -> list[Document]:
        """(Document, distance) pairs -> Documents remembering score and how they were found."""
        docs = []
        for doc, distance in results:
            # Copy the metadata so the stored documents are not modified.
            doc.metadata = {**doc.metadata, "score": round(distance, 3), "found_by": found_by}
            docs.append(doc)
        return docs

    @staticmethod
    def _name_filter(names: list[str]) -> dict:
        return {"name": names[0]} if len(names) == 1 else {"name": {"$in": names}}

    def _profiles_by_name(self, names: list[str]) -> list[Document]:
        """Fetch the layer-1 profiles of known laureates directly (no search needed)."""
        ids = [f"{self.names.stems[n]}::profile" for n in names]
        got = self.profiles.get(ids=ids, include=["metadatas", "documents"])
        by_name = {
            m["name"]: Document(page_content=text, metadata={**m, "score": 0.0, "found_by": "named"})
            for text, m in zip(got["documents"], got["metadatas"])
        }
        return [by_name[n] for n in names if n in by_name]

    # ---- the two routes ---------------------------------------------------
    def _route_a(self, question: str, names: list[str]) -> list[Document]:
        """Named laureates: their profiles + the best chunks of EACH of them."""
        docs = self._profiles_by_name(names)
        # Split the chunk budget so a comparison ("Bohr vs Einstein") gets
        # chunks about every person, not only the best-matching one.
        k_each = max(2, SECTION_K // len(names))
        for name in names:
            docs += self._tag(
                self.sections.similarity_search_with_score(question, k=k_each, filter={"name": name}),
                "route A",
            )
        return docs

    def _route_b(self, question: str, category: str | None) -> tuple[list[str], list[Document]]:
        """No name: layer 1 picks the laureates, then layer 2 is searched only within them."""
        flt = {CATEGORY_FLAGS[category][1]: True} if category else None
        hits = self.profiles.similarity_search_with_score(question, k=PROFILE_K, filter=flt)
        if not hits and flt:  # the filter was too strict; search all profiles
            hits = self.profiles.similarity_search_with_score(question, k=PROFILE_K)
        profiles = self._tag(hits, "profile search")
        names = [d.metadata["name"] for d in profiles]
        # Ask for extra chunks, then keep at most MAX_CHUNKS_EACH per laureate.
        # Otherwise one long article (Crick's six "DNA structure" chunks) can
        # fill every slot and push out Watson and Wilkins.
        candidates = self._tag(
            self.sections.similarity_search_with_score(question, k=SECTION_K * 3, filter=self._name_filter(names)),
            "route B",
        )
        chunks, per_name = [], Counter()
        for doc in candidates:  # already sorted best first
            name = doc.metadata["name"]
            if per_name[name] < MAX_CHUNKS_EACH:
                per_name[name] += 1
                chunks.append(doc)
            if len(chunks) == SECTION_K:
                break
        return names, profiles + chunks

    # ---- public entry point -----------------------------------------------
    def retrieve(self, question: str) -> RetrievalResult:
        names = self.names.find(question)
        if names:
            result = RetrievalResult(question, "A", names, docs=self._route_a(question, names))
        else:
            category = find_category(question)
            names, docs = self._route_b(question, category)
            result = RetrievalResult(question, "B", names, category, docs)

        # Safety net: catches details that no profile mentions (e.g. "who was
        # imprisoned?"), or a laureate the name matcher did not recognise.
        safety = self._tag(self.sections.similarity_search_with_score(question, k=SAFETY_NET_K), "safety net")
        if result.route == "A":
            # The question is about specific people, so only keep extra chunks
            # that mention them (e.g. Lorentz's article talking about
            # Einstein), not look-alikes ("Where was Max Born born?" would
            # otherwise bring in Max Perutz's early life).
            # Case-sensitive on purpose: "Born" the name, not "was born".
            surnames = {tokenize(_NAME_NOISE.sub("", n))[-1] for n in names}
            safety = [d for d in safety if surnames & set(tokenize(d.page_content))]

        # Merge: profiles first (short facts about who), then every chunk
        # sorted by distance (smaller = closer), without duplicates.
        profiles = [d for d in result.docs if d.metadata["layer"] == "profile"]
        chunks, seen = [], set()
        for doc in sorted((d for d in result.docs + safety if d.metadata["layer"] == "section"),
                          key=lambda d: d.metadata["score"]):
            key = (doc.metadata["file_stem"], doc.metadata["section_no"], doc.metadata["chunk_no"])
            if key not in seen:
                seen.add(key)
                chunks.append(doc)
        result.docs = profiles + chunks
        return result


# ===========================================================================
# Turning documents into the text the chat model reads
# ===========================================================================
def source_label(doc: Document) -> str:
    """'Abdus Salam — Government work' or 'Abdus Salam — profile'."""
    part = "profile" if doc.metadata["layer"] == "profile" else doc.metadata["heading"]
    return f"{doc.metadata['name']} — {part}"


def format_context(docs: list[Document], max_chars: int = MAX_CONTEXT_CHARS) -> tuple[str, list[Document]]:
    """
    Number the documents [1], [2], ... so the model can cite them, and stop
    before the text gets longer than max_chars. Returns the context text and
    the documents that were actually used (to show as sources).
    """
    parts, used, total = [], [], 0
    for doc in docs:
        text = doc.page_content
        if doc.metadata["layer"] == "profile":
            text = text[:PROFILE_CONTEXT_CHARS]
        block = f"[{len(used) + 1}] {source_label(doc)}\n{text}"
        if total + len(block) > max_chars and used:
            break
        parts.append(block)
        used.append(doc)
        total += len(block)
    return "\n\n".join(parts), used


# ===========================================================================
# Manual check: python -m chatbot.retriever ["question"]
# ===========================================================================
DEMO_QUESTIONS = [
    "What did Abdus Salam work on?",                     # Route A, one person
    "Compare the work of Niels Bohr and Einstein",       # Route A, two people
    "What did the UNHCR do to win the prize?",           # Route A, alias
    "Who discovered the structure of DNA?",              # Route B
    "Which Peace Prize laureates campaigned for women's education?",  # Route B + category
    "Which laureate was imprisoned for their activism?",  # safety net matters
    "Where was Max Born born?",                          # 'Born' the name vs 'born' the word
]


def _show(result: RetrievalResult):
    print(f"\nQ: {result.question}")
    extra = f", category filter: {result.category}" if result.category else ""
    print(f"   route {result.route}, laureates: {', '.join(result.laureates)}{extra}")
    for doc in result.docs:
        m = doc.metadata
        print(f"   {m['score']:.3f}  {m['found_by']:<14}  {source_label(doc)[:80]}")
    context, used = format_context(result.docs)
    print(f"   context: {len(context)} characters from {len(used)} of {len(result.docs)} documents")


if __name__ == "__main__":
    retriever = LaureateRetriever()
    for q in sys.argv[1:] or DEMO_QUESTIONS:
        _show(retriever.retrieve(q))
