"""
Read biographical details out of the Wikipedia ARTICLE TEXT, to fill the
gaps in the infobox.

The infobox is short and uneven: of our 1017 laureates only 49 list their
parents, 168 their citizenship and 508 a spouse, and education is often a
list of schools with no degrees. The article text ("Early life and
education", "Personal life"...) usually says it in prose:

    "Salam was the son of Chaudhary Muhammad Hussain, a school teacher..."
    "He received his BA from Government College Lahore in 1944..."

How it works, for each laureate:
  1. Pick the biographical part of the article: the Introduction plus the
     sections about early life, education, family and personal life
     (at most MAX_TEXT_CHARS characters).
  2. The local model fills in two small forms (one big form made it skip
     fields): Family = birthplace, citizenship, parents, spouses, children,
     religion; Education = every school with its degree. Code then labels
     each degree undergraduate / graduate / doctorate from its name.
  3. CHECK: every name the model wrote (school, parent, city, citizenship...)
     must appear in the text it was given. A small model sometimes fills a
     field from memory or invents it; values that fail the check are
     dropped and counted in the "dropped" column.
     Spouses and number of children also get a simple pattern search
     ("X married Y", "four children"), because the model often misses them.
  4. Combine with the infobox in laureate_details.csv: the infobox value is
     kept when it exists, the text value fills the gap, and a *_source
     column says which one was used.

The model's answers are saved one line per laureate in
laureate_details_raw.jsonl, so a stopped run continues where it left off,
and the CSV can be rebuilt without asking the model again.

Speed: ~30-60 s per laureate (two calls) on a GTX 1650, so all ~900
articles take roughly 8-12 hours: run it overnight; it resumes after Ctrl+C. Laureates without article text (~108) are skipped.

Run (Ollama must be running):
    D:\\python_evenv\\Scripts\\python.exe -m chatbot.extract_details --limit 5      # try 5 laureates
    D:\\python_evenv\\Scripts\\python.exe -m chatbot.extract_details                # all (resumes)
    D:\\python_evenv\\Scripts\\python.exe -m chatbot.extract_details Abdus_Salam    # one file, shows the result
    D:\\python_evenv\\Scripts\\python.exe -m chatbot.extract_details --redo ...     # ask again even if saved
    D:\\python_evenv\\Scripts\\python.exe -m chatbot.extract_details --csv-only     # rebuild the CSV only
"""

import csv
import json
import re
import sys
import time
from datetime import datetime
from typing import Literal, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from chatbot.cleaning import load_record
from chatbot.config import CHAT_MODEL, DATA_DIR, DETAILS_CACHE, DETAILS_CSV
from chatbot.llm import get_llm
from chatbot.retriever import fold

MAX_TEXT_CHARS = 7000      # biographical text given to the model (~1800 tokens)
INTRO_CHARS = 1500         # how much of the Introduction is included
MIN_SECTION_CHARS = 1000   # each biographical section gets an equal share of
                           # the rest, but at least this much, so "Personal
                           # life" isn't crowded out by a long "Early life"

# Sections that usually hold birth, family, education and personal life.
_BIO_HEADING = re.compile(
    r"early|youth|childhood|education|study|studies|family|personal|background|biography|"
    r"\blife\b|upbringing|origins|marriage", re.I)


# ===========================================================================
# The form the model fills in
# ===========================================================================
class School(BaseModel):
    institution: str = Field(description="School or university name, as written in the text")
    degree: Optional[str] = Field(None, description=(
        "The degree TYPE with its subject, as written, e.g. 'BS in chemical engineering', "
        "'bachelor's degree', 'MA', 'PhD in physics'; null if not stated"))
    level: Literal["school", "undergraduate", "graduate", "doctorate", "unknown"] = Field(
        "unknown", description="school = before university; undergraduate = bachelor's/first degree; "
                               "graduate = master's; doctorate = PhD/DSc/MD; unknown if not stated")
    year: Optional[int] = Field(None, description="Year the degree was completed, if stated")


class Family(BaseModel):
    """Form 1 of 2: birth, nationality, family and personal life.
    One big form for everything made the 4B model skip fields (Pauling's wife
    and four children were in the text but left empty), so it's two smaller forms."""
    # The descriptions name the phrases to look for: a small model finds much
    # more when told what the sentence usually looks like.
    birth_city: Optional[str] = Field(None, description="Town or city of birth ('was born in X')")
    birth_country: Optional[str] = Field(None, description="Country or region of birth, as written")
    citizenships: list[str] = Field(default_factory=list, description=(
        "Nationality words as written, e.g. 'Pakistani' from 'a Pakistani physicist', "
        "'American' from 'an American chemist', or 'British citizen'"))
    father_name: Optional[str] = Field(None, description="From 'son of X', 'daughter of X', 'born to X', 'his father X'")
    father_occupation: Optional[str] = Field(None, description="e.g. 'school teacher', 'physician'")
    mother_name: Optional[str] = Field(None, description="From 'and X', 'his mother X', 'née X'")
    mother_occupation: Optional[str] = None
    spouses: list[str] = Field(default_factory=list,
                               description="Names of husbands or wives, from 'married X' or 'his wife X'")
    children_count: Optional[int] = Field(None, description="Number of children as a number ('four children' -> 4)")
    religion: Optional[str] = Field(None, description="Religion or religious community, e.g. 'Ahmadiyya', 'Jewish', 'Catholic'")


class Education(BaseModel):
    """Form 2 of 2: schools and degrees."""
    education: list[School] = Field(default_factory=list, description=(
        "Every school, college and university the person studied at, in order. "
        "Only educational institutions, never a city."))


SYSTEM = """You extract biographical facts from Wikipedia text into JSON.
Rules:
- Use ONLY the given text. If a fact is not stated, leave it null or empty. Never guess.
- Copy names of people, places and schools exactly as they are written in the text.
- Facts must be about the named person, not about their students, colleagues or children.
- Read the whole text, including the Personal life part."""

FAMILY_PROMPT = """Person: {name}

Text:
{text}

From the text, fill in about {name}: where they were born, their nationality, their father and
mother (name and job), religion, the names of their husbands or wives, and how many children they had."""

EDUCATION_PROMPT = """Person: {name}

Text:
{text}

From the text, list every school, college and university {name} studied at, with the degree
earned there (e.g. 'BS in chemical engineering', 'PhD in physics') and the year, if stated."""


# ===========================================================================
# Step 1: the biographical part of the article
# ===========================================================================
def _cut(text: str, limit: int) -> str:
    """Shorten to at most limit characters, ending at a full sentence."""
    if len(text) <= limit:
        return text
    end = text.rfind(". ", 0, limit)
    return text[:end + 1] if end > limit // 2 else text[:limit]


def bio_text(record: dict) -> str:
    sections = record["content_sections"]
    if not sections:
        return ""
    parts = []
    intro = [s for s in sections if s["heading"].lower() == "introduction"]
    if intro:
        parts.append(_cut(intro[0]["text"], INTRO_CHARS))
    bio = [s for s in sections if s["heading"].lower() != "introduction" and _BIO_HEADING.search(s["heading"])]
    if not bio:  # no clearly named section: use the first one after the intro
        bio = [s for s in sections if s["heading"].lower() != "introduction"][:1]
    share = max(MIN_SECTION_CHARS, (MAX_TEXT_CHARS - sum(map(len, parts))) // max(len(bio), 1))
    for s in bio:
        parts.append(f"## {s['heading']}\n{_cut(s['text'], share)}")
    return _cut("\n\n".join(parts), MAX_TEXT_CHARS)


# ===========================================================================
# Step 3: keep only values that really appear in the text
# ===========================================================================
# The degree's level is decided by code from the degree's name, which is
# more reliable than the model's label. Checked in this order (M.D. is a
# doctorate, not a master's).
_LEVELS = [
    # Word boundaries matter: "and philosophy" must not read as "D.Phil".
    ("doctorate", re.compile(r"\bph\.?\s?d\b|doctor|\bd\.?\s?sc\b|\bd\.?\s?phil\b|\bm\.?\s?d\b|\bsc\.?\s?d\b|"
                             r"habilitation", re.I)),
    ("undergraduate", re.compile(r"bachelor|undergrad|first degree|tripos|first.class|honours degree|"
                                 r"\bb\.?\s?(?:a|s|sc|eng|ch|ed)\b|"
                                 r"\ba\.?\s?b\b|\bs\.?\s?b\b|ll\.?\s?b|\bm\.?\s?b\b", re.I)),
    ("graduate", re.compile(r"master|\bm\.?\s?(?:a|s|sc|phil|eng)\b|diplom|licen", re.I)),
]
_SCHOOL = re.compile(r"primary|elementary|secondary|high school|grammar school|gymnas|lyc[eé]e|"
                     r"intermediate|matricul|\bf\.?\s?a\b", re.I)
_OXBRIDGE = re.compile(r"Oxford|Cambridge", re.I)


def degree_level(school: dict) -> str:
    degree, name = school.get("degree") or "", school["institution"]
    for level, pattern in _LEVELS:
        if pattern.search(degree):
            # Oxford/Cambridge give their BA graduates an MA without more study.
            if level == "graduate" and _OXBRIDGE.search(name) and re.search(r"\bm\.?\s?a\b", degree, re.I):
                return "undergraduate"
            return level
    if _SCHOOL.search(degree) or _SCHOOL.search(name):
        return "school"
    return school.get("level") or "unknown"

_SKIP_WORDS = {"the", "and", "for", "von", "van", "der", "des", "del", "with"}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", fold(text).lower())


def found_in(value, text_words: set[str]) -> bool:
    """True if every meaningful word of value appears in the text.
    'University of Cambridge' passes when the text says 'Cambridge University'."""
    if value is None or value == "":
        return False
    words = [w for w in _words(str(value)) if len(w) >= 3 and w not in _SKIP_WORDS]
    words = words or _words(str(value))  # short names such as "Wu"
    return bool(words) and all(w in text_words for w in words)


# Spouses and children: the model missed them even when the text said
# "Pauling married Ava Helen Miller ... They had four children", so these two
# fields also get a pattern search. "married" only counts in a sentence about
# the laureate (it starts with He/She or contains the surname), so "his
# daughter Linda married Barclay Kamb" is not taken as Pauling's wife.
_NUMBERS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve".split())}
_TITLE = r"(?:(?:Dr|Mrs|Mr|Miss|Ms|Lady|Sir|Professor|Prof)\.?\s+)?"   # "married Dr. Susan Carol Marks"
_NAME = r"((?:[A-Z][\w'-]+\.?\s?){1,4})"
_MARRIED = re.compile(r"\bmarried\s+(?:to\s+)?" + _TITLE + _NAME)
_SPOUSE_WORD = re.compile(r"\b(?:his wife|her husband),?\s+" + _TITLE + _NAME)
_CHILDREN = re.compile(r"\b(\d{1,2}|" + "|".join(_NUMBERS) + r")\s+(?:children|sons and daughters)", re.I)
# Sentence ends, but not after a title: "he married Dr. Susan Carol Marks".
_SENTENCE = re.compile(r"(?<!Dr\.)(?<!Mr\.)(?<!Mrs\.)(?<!Ms\.)(?<!St\.)(?<=[.!?])\s+(?=[A-Z])")
# A relative named before "married" means someone else's marriage.
_RELATIVE = re.compile(r"\b(?:daughters?|sons?|brothers?|sisters?|father|mother|parents|children)\b", re.I)


def spouses_from_text(text: str, name: str) -> list[str]:
    surname = fold(name).split()[-1].lower()
    found = []
    for sentence in _SENTENCE.split(text):
        about_laureate = re.match(r"\s*(?:He|She|In \d{4},? (?:he|she))\b", sentence) or surname in fold(sentence).lower()
        if not about_laureate:
            continue
        for m in [*_MARRIED.finditer(sentence), *_SPOUSE_WORD.finditer(sentence)]:
            spouse = m.group(1).strip(" .")
            if (_RELATIVE.search(sentence[:m.start()]) or spouse in found
                    or spouse.split()[0] in ("In", "On", "The", "A", "His", "Her")):
                continue
            found.append(spouse)
    return found


def children_from_text(text: str) -> Optional[int]:
    m = _CHILDREN.search(text)
    if not m:
        return None
    word = m.group(1).lower()
    return int(word) if word.isdigit() else _NUMBERS[word]


def verify(d: dict, text: str) -> tuple[dict, list[str]]:
    """d = both forms merged. Return (checked details, list of dropped values)."""
    words = set(_words(text))
    dropped = []

    def keep(value):
        if value is None or value == "":
            return value
        if found_in(value, words):
            return value
        dropped.append(str(value))
        return None

    for key in ("birth_city", "birth_country", "father_name", "father_occupation",
                "mother_name", "mother_occupation", "religion"):
        d[key] = keep(d[key])
    for key in ("citizenships", "spouses"):
        d[key] = [v for v in (keep(x) for x in d[key]) if v]
    schools = []
    place_words = set(_words(f"{d['birth_city'] or ''} {d['birth_country'] or ''}"))
    for s in d["education"]:
        if not keep(s["institution"]):
            continue
        if set(_words(s["institution"])) <= place_words:  # "Portland" is a city, not a school
            dropped.append(f"school={s['institution']}")
            continue
        if s["year"] and str(s["year"]) not in text:
            s["year"] = None  # a year the text doesn't contain
        s["level"] = degree_level(s)
        schools.append(s)
    d["education"] = schools
    n = d["children_count"]
    if n is not None and (not 0 <= n <= 20 or not re.search(r"child|\bsons?\b|daughter", text, re.I)):
        dropped.append(f"children_count={n}")  # e.g. 4000000000000000 instead of 4
        d["children_count"] = None
    return d, dropped


def add_pattern_matches(d: dict, text: str, name: str) -> dict:
    """Fill spouses / children from the text patterns when the model left them empty."""
    d["spouses_by"] = d["children_by"] = "model"
    if not d["spouses"]:
        d["spouses"], d["spouses_by"] = spouses_from_text(text, name), "pattern"
    if d["children_count"] is None:
        d["children_count"], d["children_by"] = children_from_text(text), "pattern"
    return d


# ===========================================================================
# Step 2: ask the model (with a saved cache)
# ===========================================================================
def load_cache() -> dict[str, dict]:
    if not DETAILS_CACHE.exists():
        return {}
    cache = {}
    for line in DETAILS_CACHE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            cache[row["file_stem"]] = row  # a later line (from --redo) wins
    return cache


def make_extractors() -> list[tuple]:
    """(form, prompt, model forced to reply with that form) for both forms."""
    llm = get_llm(thinking=False, temperature=0)
    return [(form, prompt, llm.with_structured_output(form, method="json_schema"))
            for form, prompt in ((Family, FAMILY_PROMPT), (Education, EDUCATION_PROMPT))]


def extract_one(extractors: list[tuple], record: dict) -> dict:
    """Ask the model about one laureate (two calls); returns the line saved in the cache."""
    text = bio_text(record)
    row = {"file_stem": record["file_stem"], "model": CHAT_MODEL,
           "extracted_at": datetime.now().isoformat(timespec="seconds"), "text_chars": len(text)}
    if not text:
        return {**row, "status": "no_text"}
    start, details = time.time(), {}
    for form, prompt, model in extractors:
        try:
            answer = model.invoke([SystemMessage(SYSTEM),
                                   HumanMessage(prompt.format(name=record["name"], text=text))])
        except Exception as err:  # invalid JSON or Ollama error: retried on the next run
            return {**row, "status": "error", "error": f"{form.__name__}: {str(err)[:300]}"}
        details.update(answer.model_dump())
    checked, dropped = verify(details, text)
    checked = add_pattern_matches(checked, text, record["name"])
    return {**row, "status": "ok", "seconds": round(time.time() - start, 1),
            "details": checked, "dropped": dropped}


# ===========================================================================
# Step 4: combine infobox + text into laureate_details.csv
# ===========================================================================
_BORN_DATE_END = re.compile(r".*\b(?:1[5-9]\d\d|20\d\d)\b\)?")  # everything up to the last year
_AGE = re.compile(r"\(\s*(?:age|aged)\s*\d+\s*\)", re.I)


def infobox_birthplace(born: str) -> str:
    """'Richard Phillips Feynman (1918-05-11) May 11, 1918 New York City, U.S.' -> 'New York City, U.S.'"""
    place = _AGE.sub("", _BORN_DATE_END.sub("", born, count=1))
    return place.strip(" ,;.()")


def _box(record: dict, *keys: str) -> str:
    return next((record["infobox"][k] for k in keys if record["infobox"].get(k)), "")


def _pick(box_value: str, text_value: str) -> tuple[str, str]:
    """(value, source): the infobox wins when it has the field."""
    if box_value:
        return box_value, "infobox"
    if text_value:
        return text_value, "text"
    return "", ""


def _person(name, occupation) -> str:
    return ", ".join(x for x in (name, occupation) if x)


def _school(s: dict) -> str:
    extra = ", ".join(str(x) for x in (s["degree"], s["level"] if s["level"] != "unknown" else None, s["year"]) if x)
    return f"{s['institution']} ({extra})" if extra else s["institution"]


def build_csv(records: list[dict], cache: dict[str, dict]) -> list[dict]:
    rows = []
    for r in records:
        saved = cache.get(r["file_stem"], {})
        d = saved.get("details") or {}
        edu = d.get("education", [])
        city_country = ", ".join(x for x in (d.get("birth_city"), d.get("birth_country")) if x)
        father = _person(d.get("father_name"), d.get("father_occupation"))
        mother = _person(d.get("mother_name"), d.get("mother_occupation"))

        row = {"file_stem": r["file_stem"], "name": r["name"], "url": r["url"]}
        row["birth_place"], row["birth_place_source"] = _pick(infobox_birthplace(_box(r, "Born")), city_country)
        row["citizenship"], row["citizenship_source"] = _pick(
            _box(r, "Citizenship", "Nationality"), "; ".join(d.get("citizenships", [])))
        box_parents = _box(r, "Parents", "Parent", "Parent(s)")
        if not box_parents and (_box(r, "Father") or _box(r, "Mother")):
            box_parents = "; ".join(f"{k}: {_box(r, k)}" for k in ("Father", "Mother") if _box(r, k))
        text_parents = "; ".join(f"{k}: {v}" for k, v in (("Father", father), ("Mother", mother)) if v)
        row["parents"], row["parents_source"] = _pick(box_parents, text_parents)
        row["spouses"], row["spouses_source"] = _pick(
            _box(r, "Spouse", "Spouses", "Spouse(s)"), "; ".join(d.get("spouses", [])))
        row["children"], row["children_source"] = _pick(
            _box(r, "Children"), "" if d.get("children_count") is None else str(d["children_count"]))
        row["religion"] = d.get("religion") or ""          # never in the infobox
        # Education: both versions are kept, because they complement each other:
        # the infobox list is usually more complete, the text gives degrees.
        row["education_infobox"] = " ".join(_box(r, k) for k in ("Education", "Alma mater")).strip()
        row["education_text"] = "; ".join(_school(s) for s in edu)
        row["undergrad_text"] = "; ".join(dict.fromkeys(s["institution"] for s in edu if s["level"] == "undergraduate"))
        row["doctorate_text"] = "; ".join(dict.fromkeys(s["institution"] for s in edu if s["level"] == "doctorate"))
        row["extraction_status"] = saved.get("status", "not_run")
        row["dropped"] = "; ".join(saved.get("dropped", []))
        rows.append(row)

    with open(DETAILS_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return rows


def print_summary(rows: list[dict]):
    print(f"\nWrote {DETAILS_CSV.name}: {len(rows)} laureates")
    print(f"{'field':14} {'infobox':>8} {'+ text':>7} {'total':>6}")
    for field in ("birth_place", "citizenship", "parents", "spouses", "children"):
        box = sum(r[f"{field}_source"] == "infobox" for r in rows)
        text = sum(r[f"{field}_source"] == "text" for r in rows)
        print(f"{field:14} {box:8} {text:7} {box + text:6}")
    print(f"{'religion':14} {0:8} {sum(bool(r['religion']) for r in rows):7}")
    print(f"education with degrees from the text: {sum(bool(r['education_text']) for r in rows)}, "
          f"with an undergraduate school: {sum(bool(r['undergrad_text']) for r in rows)}")


# ===========================================================================
# Command line
# ===========================================================================
def main():
    args = sys.argv[1:]
    redo, csv_only = "--redo" in args, "--csv-only" in args
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else None
    stems = [a for a in args if not a.startswith("--") and not a.isdigit()]

    records = [load_record(p) for p in sorted(DATA_DIR.glob("*.json"))]
    cache = load_cache()

    if not csv_only:
        todo = [r for r in records if (not stems or r["file_stem"] in stems)
                and (redo or cache.get(r["file_stem"], {}).get("status") not in ("ok", "no_text"))]
        todo = todo[:limit] if limit else todo
        print(f"{len(todo)} laureates to read ({len(cache)} already saved in {DETAILS_CACHE.name})")
        extractors = make_extractors()
        start = time.time()
        with open(DETAILS_CACHE, "a", encoding="utf-8") as f:
            for i, record in enumerate(todo, 1):
                row = extract_one(extractors, record)
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()  # saved at once: stopping with Ctrl+C loses nothing
                cache[record["file_stem"]] = row
                left = (time.time() - start) / i * (len(todo) - i) / 60
                print(f"[{i}/{len(todo)}] {row['status']:7} {record['file_stem']:40} "
                      f"{row.get('seconds', 0):5}s  dropped {len(row.get('dropped', []))}  ~{left:.0f} min left",
                      flush=True)
                if stems:  # one named laureate: show what was found
                    print(json.dumps(row.get("details"), ensure_ascii=False, indent=1))
                    print("dropped (not in the text):", row.get("dropped"))

    print_summary(build_csv(records, cache))


if __name__ == "__main__":
    main()
