"""
The counting tool: answers "how many / youngest / oldest / list all"
questions from laureate_prizes.csv with pandas.

The chat model does NOT write pandas code (a 4B model would get it wrong
too often). It only fills in a small form, TableQuery: "count, category =
Physics, birth country = Spain". This module then runs that form exactly.

Try it:
    D:\\python_evenv\\Scripts\\python.exe -m chatbot.table_tool
"""

import re
from functools import lru_cache
from typing import Literal, Optional

import pandas as pd
from pydantic import BaseModel, Field

from chatbot.config import PRIZES_CSV

Category = Literal["Physics", "Chemistry", "Physiology or Medicine", "Literature", "Peace", "Economic Sciences"]


class TableQuery(BaseModel):
    """One question to the laureate table. Leave a filter empty (null) to not filter on it."""

    action: Literal["count", "list", "youngest", "oldest", "count_by_country", "count_by_category"] = Field(
        description="count = how many laureates match; list = name them; youngest/oldest = rank by age when "
                    "the prize was awarded; count_by_country / count_by_category = totals per group, largest first")
    category: Optional[Category] = Field(None, description="Nobel Prize category")
    birth_country: Optional[str] = Field(None, description="Country of birth, e.g. 'Spain', 'USA', 'Germany'")
    birth_place: Optional[str] = Field(None, description="City, US state or region of birth, e.g. 'New York', "
                                                         "'California', 'Paris', 'Vienna'")
    university: Optional[str] = Field(None, description="University the laureate studied at, e.g. 'Harvard'")
    undergraduate_only: bool = Field(False, description="True if the question is about undergraduate / bachelor's studies")
    gender: Optional[Literal["female", "male"]] = Field(None, description="'female' for questions about women")
    organizations: bool = Field(False, description="True only if the question is about organisations, not people")
    year_from: Optional[int] = Field(None, description="First prize year to include")
    year_to: Optional[int] = Field(None, description="Last prize year to include")
    rank: int = Field(1, description="For youngest/oldest: 1 = the youngest/oldest, 3 = the third youngest/oldest")


class TablePlan(BaseModel):
    """All table lookups needed to answer the question (usually one)."""
    queries: list[TableQuery] = Field(description="One entry per separate thing asked, at most 3")


# Ways people write countries -> the name used in the Nobel data.
COUNTRY_ALIASES = {
    "us": "USA", "u.s.": "USA", "usa": "USA", "united states": "USA", "america": "USA",
    "united states of america": "USA",
    "uk": "United Kingdom", "britain": "United Kingdom", "great britain": "United Kingdom",
    "england": "United Kingdom", "netherlands": "the Netherlands", "holland": "the Netherlands",
    "korea": "South Korea", "czechia": "Czech Republic",
}
# The Nobel data lists these separately from "United Kingdom".
UK_PARTS = {"United Kingdom", "Scotland", "Northern Ireland"}

# US birth cities are stored like "Brooklyn, NY", so a state is matched by its code.
US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "washington dc": "DC", "washington d.c.": "DC",
}
# "New York State" / "state of New York" -> "new york"
_STATE_WORDS = re.compile(r"\b(state of|state)\b", re.I)
MAX_NAMES = 40  # longest list of names handed to the chat model


@lru_cache(maxsize=1)
def load_table() -> pd.DataFrame:
    df = pd.read_csv(PRIZES_CSV, dtype=str).fillna("")
    df["year"] = df["year"].astype(int)
    df["age"] = pd.to_numeric(df["age_at_award"], errors="coerce")
    # Exact age in days ranks people of the same age; if the birth day is
    # unknown, fall back to whole years.
    df["age_days"] = pd.to_numeric(df["age_days_at_award"], errors="coerce").fillna(df["age"] * 365.25)
    return df


# Category words in a question -> category. Used to repair a plan.
_CATEGORY_WORDS = {
    "Physics": r"physics|physicist", "Chemistry": r"chemistry|chemist",
    "Physiology or Medicine": r"medicine|physiology|medical", "Literature": r"literature|writer|novelist|poet",
    "Peace": r"peace", "Economic Sciences": r"economic|economics|economist",
}
_YEAR_WORDS = re.compile(r"\b(1[89]\d\d|20\d\d|since|after|before|between|until|last|recent|decade|century|from \d)", re.I)


_UNDERGRAD_WORDS = re.compile(r"under\s?grad|bachelor|first degree|\bB\.?A\.?\b|\bB\.?Sc?\.?\b", re.I)


def _is_country(name: str) -> bool:
    df = load_table()
    n = COUNTRY_ALIASES.get(name.strip().lower(), name.strip()).lower()
    return n in set(df["birth_country"].str.lower()) | set(df["birth_country_then"].str.lower())


def fix_plan(plan: TablePlan, question: str) -> TablePlan:
    """
    Small models sometimes fill the form wrongly. These checks compare the
    form with the question itself, which code can do reliably:
      * the question names exactly one prize category but no query has it -> add it
      * the question mentions no year or period -> remove year filters
      * "undergrad" / "bachelor" in the question <-> undergraduate_only
      * a birth "country" that is not a country ("New York") -> birth_place
      * "count by country" for one named place, when the question doesn't
        compare countries ("born in New York" -> plain count)
    """
    q = question.lower()
    named = [c for c, pattern in _CATEGORY_WORDS.items() if re.search(rf"\b(?:{pattern})\b", q)]
    per_group = "by category" in q or "each category" in q or "per category" in q
    compares_countries = bool(re.search(r"countr|\bmost\b|\beach\b|\bper\b|rank", q))
    undergrad = bool(_UNDERGRAD_WORDS.search(question))
    for query in plan.queries:
        if (query.action == "count_by_country" and (query.birth_country or query.birth_place)
                and not compares_countries):
            query.action = "count"
        if len(named) == 1 and not per_group and query.action != "count_by_category" and not any(
                x.category for x in plan.queries):
            query.category = named[0]
        if not _YEAR_WORDS.search(question):
            query.year_from = query.year_to = None
        if query.university:
            query.undergraduate_only = undergrad
        if query.birth_country and not _is_country(query.birth_country):
            query.birth_place, query.birth_country = query.birth_place or query.birth_country, None
    return plan


def _country_mask(df: pd.DataFrame, country: str) -> pd.Series:
    wanted = COUNTRY_ALIASES.get(country.strip().lower(), country.strip())
    if wanted == "United Kingdom":
        return df["birth_country"].isin(UK_PARTS)
    return (df["birth_country"].str.lower() == wanted.lower()) | (df["birth_country_then"].str.lower() == wanted.lower())


def _place_mask(df: pd.DataFrame, place: str) -> tuple[pd.Series, str]:
    """
    Birth city / US state / region. A US state name matches every city in
    that state ("New York" -> "New York, NY", "Brooklyn, NY", "Yonkers, NY").
    Anything else is searched in the city and full birthplace text.
    """
    key = _STATE_WORDS.sub("", place).strip().lower()
    if key in US_STATES:
        code = US_STATES[key]
        mask = df["birth_city"].str.contains(rf",\s*{code}$", regex=True) & (df["birth_country"] == "USA")
        return mask, f"born in the US state of {place} (any city with ', {code}')"
    mask = (df["birth_city"].str.contains(place, case=False, regex=False)
            | df["birth_location"].str.contains(place, case=False, regex=False))
    return mask, f"born in a place matching '{place}'"


def _filter(q: TableQuery, trace=None) -> tuple[pd.DataFrame, list[str]]:
    """
    Apply the filters; also return a plain-English description of them.
    With a trace, log how many laureates are left after each filter, so a
    surprising result (e.g. 0) shows exactly which filter removed everyone.
    """
    df, said = load_table(), []

    def step(label: str, mask):
        nonlocal df
        df = df[mask]
        said.append(label)
        if trace:
            trace.add("table.filter", filter=label, laureates_left=int(df["nobel_id"].nunique()))

    step("organisations" if q.organizations else "people (organisations excluded)",
         df["kind"] == ("organization" if q.organizations else "person"))
    if q.category:
        step(f"category {q.category}", df["category"] == q.category)
    if q.birth_country:
        step(f"born in {q.birth_country} (country borders of today)", _country_mask(df, q.birth_country))
    if q.birth_place:
        mask, label = _place_mask(df, q.birth_place)
        step(label, mask)
    if q.university:
        column = "undergrad_schools" if q.undergraduate_only else "education"
        step(f"{'undergraduate degree' if q.undergraduate_only else 'studied'} at {q.university}",
             df[column].str.contains(q.university, case=False, regex=False))
    if q.gender:
        step(q.gender, df["gender"] == q.gender)
    if q.year_from:
        step(f"prize year ≥ {q.year_from}", df["year"] >= q.year_from)
    if q.year_to:
        step(f"prize year ≤ {q.year_to}", df["year"] <= q.year_to)
    return df, said


def _age_text(row) -> str:
    """'25 years 209 days' (or '25 years' when the birth day is unknown)."""
    if pd.isna(row["age"]):
        return ""
    if not row["age_days_at_award"]:  # birth day unknown
        return f"{int(row['age'])} years"
    # Days since the last birthday before the award.
    born = pd.Timestamp(row["birth_date"])
    awarded = born + pd.Timedelta(days=int(row["age_days_at_award"]))
    last_birthday = born + pd.DateOffset(years=int(row["age"]))
    return f"{int(row['age'])} years {(awarded - last_birthday).days} days"


def _person_line(row) -> str:
    age = f", age {_age_text(row)} at award" if pd.notna(row["age"]) else ""
    born = f", born {row['birth_date']} in {row['birth_country']}" if row["birth_date"] else ""
    return f"{row['name']} — {row['category']} {row['year']}{age}{born}"


def run_query(q: TableQuery, trace=None) -> str:
    """Run one TableQuery and describe the result in text the chat model can read."""
    if trace:
        trace.add("table.query", query=q.model_dump(exclude_defaults=True))
    result = _run_query(q, trace)
    if trace:
        trace.add("table.result", text=result)
    return result


def _run_query(q: TableQuery, trace=None) -> str:
    df, said = _filter(q, trace)
    header = f"Table lookup: {q.action}; filters: {', '.join(said)}."
    laureates = df.drop_duplicates("nobel_id")  # a double winner counts once

    if q.action in ("youngest", "oldest"):
        ranked = df.dropna(subset=["age"]).sort_values("age_days", ascending=q.action == "youngest")
        if ranked.empty:
            return header + "\nNo matching laureates."
        top = ranked.head(max(q.rank, 3))
        pick = top.iloc[min(q.rank, len(top)) - 1]
        ordinal = {1: "", 2: "2nd ", 3: "3rd "}.get(q.rank, f"{q.rank}th ")
        lines = [f"{i}. {_person_line(r)}" for i, (_, r) in enumerate(top.iterrows(), 1)]
        # The ANSWER line comes first and is self-contained, so the model
        # doesn't have to work out the rank from the list itself.
        return (f"{header}\nANSWER: the {ordinal}{q.action} laureate is {_person_line(pick)}.\n"
                f"Ranking by exact age (years and days) when the prize was awarded:\n" + "\n".join(lines))

    if q.action in ("count_by_country", "count_by_category"):
        column = "birth_country" if q.action == "count_by_country" else "category"
        values = laureates[column].replace({part: "United Kingdom" for part in UK_PARTS})
        counts = values[values != ""].value_counts().head(15)
        lines = [f"{k}: {v}" for k, v in counts.items()]
        return f"{header}\nTotal laureates: {len(laureates)}\nTop groups:\n" + "\n".join(lines)

    # count / list
    names = [_person_line(r) if not q.organizations else f"{r['name']} — {r['category']} {r['year']}"
             for _, r in laureates.sort_values("year").iterrows()]
    shown = names[:MAX_NAMES]
    more = f"\n... and {len(names) - MAX_NAMES} more" if len(names) > MAX_NAMES else ""
    note = ""
    if q.birth_place and q.birth_place.strip().lower() in ("new york", "new york state"):
        city = laureates["birth_city"].eq("New York, NY").sum()
        note += f"\nNote: this counts the whole state of New York; {city} of them were born in New York City itself."
    if q.university:
        note += ("\nNote: university data comes from Wikipedia infoboxes and is incomplete; "
                + ("'undergraduate' is only known when a bachelor's degree (BA, AB, BS...) is listed."
                   if q.undergraduate_only else "this counts any study (bachelor's, master's or doctorate)."))
    return f"{header}\nCount: {len(laureates)}{note}\nLaureates:\n" + "\n".join(shown) + more


def run_plan(plan: TablePlan, trace=None) -> str:
    return "\n\n".join(run_query(q, trace) for q in plan.queries[:3])


if __name__ == "__main__":
    for q in [
        TableQuery(action="count", birth_country="Spain"),
        TableQuery(action="count", university="Harvard", undergraduate_only=True),
        TableQuery(action="youngest", rank=3),
        TableQuery(action="oldest"),
        TableQuery(action="count", gender="female", category="Physics"),
        TableQuery(action="count_by_country"),
    ]:
        print(run_query(q)[:700], "\n")
