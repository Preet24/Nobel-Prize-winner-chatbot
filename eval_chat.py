"""
Test questions for the chatbot: asks each question through the same code
path the Streamlit app uses (chat.prepare_turn + chat.stream_answer) and
checks the answer for words that must appear.

Run (Ollama must be running; takes ~15-30 minutes on a GTX 1650):
    D:\\python_evenv\\Scripts\\python.exe eval_chat.py
    D:\\python_evenv\\Scripts\\python.exe eval_chat.py 3 7      # only cases 3 and 7

Writes eval_results.md with every question, answer, sources and verdict.

How a case is checked:
  expect      every regex in the list must appear in the answer (case and
              accents ignored); "a|b" means either word is fine
  refuse      the model must say it could not find the answer
  follow_up   this question is asked first and its real answer becomes the
              chat history, so pronouns ("she", "he") can be tested
  route       "text" or "table": the tool the question must be sent to
  known_limit not scored: shows something the bot is known not to do yet
"""

import re
import sys
import time

from chatbot.chat import prepare_turn, stream_answer
from chatbot.config import CHAT_MODEL, PROJECT_ROOT
from chatbot.llm import get_llm
from chatbot.retriever import LaureateRetriever, fold, source_label

REFUSAL = r"could not find|couldn't find|cannot find|can't find|not (?:find|found|contain|mention|include|provide)|no information|not available|unable to"

CASES = [
    # --- Route A: a named laureate with a full article --------------------
    {"q": "What did Abdus Salam work on?", "expect": [r"electroweak"]},
    {"q": "Why is Linus Pauling unusual among Nobel laureates?", "expect": [r"chemistry", r"peace"]},
    {"q": "What movement did Wangari Maathai found?", "expect": [r"green belt"]},
    {"q": "Why was Liu Xiaobo in prison when he received the Peace Prize?", "expect": [r"charter 08|subversion|inciting"]},
    {"q": "What job did Kofi Annan hold before winning the Peace Prize?", "expect": [r"secretary.general"]},
    {"q": "Where did Albert Schweitzer build his hospital?", "expect": [r"lambar[eé]n[eé]|gabon"]},
    {"q": "Which novels is Kazuo Ishiguro best known for?", "expect": [r"remains of the day|never let me go"]},
    # --- Route A: profile-only laureates (no article text in the data) ----
    {"q": "Which Nobel Prizes did Marie Curie win and when?", "expect": [r"physics", r"chemistry", r"1903", r"1911"]},
    {"q": "What did Alexander Fleming discover?", "expect": [r"penicillin"]},
    {"q": "What is Richard Feynman known for?", "expect": [r"quantum electrodynamics|feynman diagram|path integral"]},
    {"q": "Where was Malala Yousafzai born?", "expect": [r"mingora|swat|pakistan"]},
    {"q": "In which year did Nelson Mandela receive the Nobel Peace Prize?", "expect": [r"1993"]},
    {"q": "When was Lech Walesa born and which prize did he win?", "expect": [r"1943", r"peace", r"1983"]},  # no accents typed
    {"q": "When did Barack Obama win the Nobel Peace Prize?", "expect": [r"2009"]},  # from prize_overrides.csv
    # --- Organisations and aliases ----------------------------------------
    {"q": "Who founded the Grameen Bank?", "expect": [r"yunus"]},
    {"q": "Why did the UNHCR win the Nobel Peace Prize?", "expect": [r"refugee"]},
    # --- Route B: no name in the question ---------------------------------
    {"q": "Who discovered the structure of DNA?", "expect": [r"watson", r"crick"]},
    {"q": "Which Peace Prize laureate campaigned for girls' education in Pakistan?", "expect": [r"malala"]},
    {"q": "Who won the Nobel Prize in Literature in 2016?", "expect": [r"dylan"]},
    # --- Follow-up questions (pronouns) -----------------------------------
    {"follow_up": "Tell me about Marie Curie.", "q": "When did she die?", "expect": [r"1934"]},
    {"follow_up": "Who founded the Grameen Bank?", "q": "When did he win the Nobel Prize?", "expect": [r"2006"]},
    # --- Questions the data cannot answer: the bot must say so ------------
    # The table tool may answer this one ("no laureates in 2031"): also correct.
    {"q": "Who won the Nobel Prize in Physics in 2031?",
     "expect": [r"could not find|couldn't find|no (?:laureates?|one|winners?|nobel)|not (?:yet )?(?:been )?awarded|\b0\b"]},
    {"q": "What is the capital of France?", "refuse": True},
    {"q": "What is Albert Einstein's favourite pizza topping?", "refuse": True},
    # --- Counting / ranking: must use the table route ---------------------
    {"q": "How many nobel prize winner did there undergrads from harvard", "route": "table", "expect": [r"\b29\b"]},
    {"q": "who is the third youngest and oldest nobel prize winner", "route": "table", "expect": [r"bragg", r"goodenough"]},
    {"q": "How many nobel prize winner did spain has", "route": "table", "expect": [r"\b7\b|seven"]},
    {"q": "How many women have won the Nobel Prize in Physics?", "route": "table", "expect": [r"\b5\b|five"]},
    {"q": "Who was the youngest Physics laureate?", "route": "table", "expect": [r"bragg"]},
    {"q": "Which country has the most Nobel laureates?", "route": "table", "expect": [r"usa|united states"]},
    {"q": "How many organisations have won the Nobel Peace Prize?", "route": "table", "expect": [r"\b28\b"]},
    {"follow_up": "How many laureates were born in Spain?", "q": "And how many in Italy?", "route": "table", "expect": [r"\b20\b|twenty"]},
    {"q": "How many nobel prizer winner did thier undergrad from oxford", "route": "table", "expect": [r"\b16\b|sixteen"]},
    {"q": "How many nobel prize winner were born in new york", "route": "table", "expect": [r"\b74\b"]},
    # "most" here is not a counting question: must stay on the text route
    {"q": "What is Marie Curie most famous for?", "route": "text", "expect": [r"radioactiv|polonium|radium"]},
]


def ask(llm, retriever, question: str, history: list[dict]):
    """One chat turn exactly like the app: retrieve, then stream the answer."""
    turn = prepare_turn(question, history, retriever)
    answer = "".join(text for kind, text in stream_answer(llm, turn) if kind == "answer")
    turn.trace.finish(answer)  # every test turn is logged too (logs/turns.jsonl)
    return turn, answer.strip()


def check(case: dict, answer: str, route: str) -> tuple[str, str]:
    """Return (verdict, reason)."""
    text = fold(answer).lower()
    if case.get("route") and case["route"] != route:
        return "FAIL", f"used the {route} route, expected {case['route']}"
    if case.get("known_limit"):
        return "INFO", "known limit, not scored"
    if case.get("refuse"):
        return ("PASS", "refused as expected") if re.search(REFUSAL, text) else ("FAIL", "answered instead of refusing")
    missing = [p for p in case["expect"] if not re.search(fold(p).lower(), text)]
    if missing:
        return "FAIL", "missing: " + ", ".join(missing)
    if route == "table":
        return "PASS", "all expected facts present (table)"
    if not re.search(r"\[\d+\]", answer):
        return "PASS", "correct, but no [n] citation"
    return "PASS", "all expected facts present, cited"


def main():
    chosen = {int(a) for a in sys.argv[1:]}
    retriever, llm = LaureateRetriever(), get_llm()
    report = [f"# Chatbot test results\n\nModel: `{CHAT_MODEL}`, thinking off.\n"]
    rows, details = [], []

    for no, case in enumerate(CASES, 1):
        if chosen and no not in chosen:
            continue
        start = time.time()
        history = []
        if case.get("follow_up"):
            _, first_answer = ask(llm, retriever, case["follow_up"], [])
            history = [{"role": "user", "content": case["follow_up"]},
                       {"role": "assistant", "content": first_answer}]
        turn, answer = ask(llm, retriever, case["q"], history)
        seconds = time.time() - start
        verdict, reason = check(case, answer, turn.route)

        r = turn.retrieval
        if r:  # text route: which retrieval route and laureates
            route, short = f"text, {r.route}: {', '.join(r.laureates)}", f"text/{r.route}"
            route += f" · *prize filter* {r.category}" if r.category else ""
        else:  # table route: the filled-in query forms
            route, short = "table: " + "; ".join(
                str(q.model_dump(exclude_defaults=True)) for q in turn.plan.queries), "table"
        print(f"[{no:2}] {verdict}  {seconds:5.0f}s  {case['q']}  ({reason})", flush=True)
        rows.append(f"| {no} | {verdict} | {case['q']} | {short} | {seconds:.0f}s | {reason} |")
        details.append(
            f"## {no}. {case['q']} — {verdict}\n\n"
            + (f"*Follow-up to:* {case['follow_up']}  \n" if case.get("follow_up") else "")
            + f"*Route* {route}"
            + (f"  \n*Search text:* {turn.search_query}" if turn.search_query not in ("", case["q"]) else "")
            + f"  \n*Time:* {seconds:.0f}s · *Check:* {reason}\n\n"
            + "\n".join(f"> {line}" for line in answer.splitlines()) + "\n\n"
            + ("Sources: " + "; ".join(f"[{i}] {source_label(d)}" for i, d in enumerate(turn.sources, 1))
               if turn.sources else "Table result:\n```\n" + turn.table_result[:1500] + "\n```")
            + "\n"
        )

    scored = [x for x in rows if "| INFO |" not in x]
    passed = sum("| PASS |" in x for x in scored)
    summary = f"**{passed} / {len(scored)} scored cases passed.**\n"
    print("\n" + summary.strip("*\n"))
    report += [summary, "| # | Result | Question | Route | Time | Check |", "|---|---|---|---|---|---|",
               *rows, "", *details]
    (PROJECT_ROOT / "eval_results.md").write_text("\n".join(report), encoding="utf-8")
    print("Wrote eval_results.md")


if __name__ == "__main__":
    main()
