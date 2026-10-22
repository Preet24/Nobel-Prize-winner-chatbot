"""
Logging for chat turns, so a wrong answer can be traced to the step that
caused it.

Every turn gets a Trace: an ordered list of steps, e.g.

    route      -> table   (matched "how many")
    plan.raw   -> what the model filled into the form
    plan.fixed -> the form after fix_plan() repaired it
    table.filter (one per filter) -> how many laureates are left after it
    table.result
    answer     -> the final text and how long it took

Each step is written to two files in logs/ as it happens:
  * logs/chatbot.log  : readable lines, one per step
  * logs/turns.jsonl  : one JSON record per finished turn (all steps)

Read the latest turns:
    D:\\python_evenv\\Scripts\\python.exe -m chatbot.tracing        # last 3 turns
    D:\\python_evenv\\Scripts\\python.exe -m chatbot.tracing 10     # last 10 turns
"""

import json
import logging
import sys
import time
import uuid
from datetime import datetime
from logging.handlers import RotatingFileHandler

from chatbot.config import LOG_DIR

LOG_FILE = LOG_DIR / "chatbot.log"
TURNS_FILE = LOG_DIR / "turns.jsonl"


def get_logger() -> logging.Logger:
    """The 'chatbot' logger, writing to logs/chatbot.log (set up on first use)."""
    logger = logging.getLogger("chatbot")
    if not logger.handlers:
        LOG_DIR.mkdir(exist_ok=True)
        # Rotate at 5 MB and keep 3 old files, so the log can't grow forever.
        handler = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False  # don't also print to the console / Streamlit
    return logger


def _short(value, limit: int = 400) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + f"... (+{len(text) - limit} chars)"


class Trace:
    """The record of one chat turn. Call .add(step, **data) at each step."""

    def __init__(self, question: str):
        self.id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.question = question
        self.started = time.time()
        self.steps: list[dict] = []
        self.log = get_logger()
        self.log.info(f"[{self.id}] QUESTION {question!r}")

    def add(self, step: str, **data):
        entry = {"step": step, "t": round(time.time() - self.started, 2), **data}
        self.steps.append(entry)
        details = " ".join(f"{k}={_short(v)}" for k, v in data.items())
        self.log.debug(f"[{self.id}] {step:<14} {details}")

    def error(self, step: str, err: Exception):
        self.add(step, error=f"{type(err).__name__}: {err}")
        self.log.exception(f"[{self.id}] {step} failed")

    def finish(self, answer: str):
        """Log the answer and append the whole turn to logs/turns.jsonl."""
        self.add("answer", text=answer, seconds=round(time.time() - self.started, 1))
        record = {"id": self.id, "question": self.question, "steps": self.steps}
        with open(TURNS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def as_text(self) -> str:
        """Readable version, used by the app's debug panel and the viewer below."""
        lines = [f"Turn {self.id}: {self.question}"]
        lines += [format_step(s) for s in self.steps]
        return "\n".join(lines)


def format_step(s: dict) -> str:
    data = {k: v for k, v in s.items() if k not in ("step", "t")}
    body = "\n".join(f"      {k}: {_short(v, 2000)}" for k, v in data.items())
    return f"  {s['t']:>6.2f}s  {s['step']}\n{body}"


# ===========================================================================
# Viewer: python -m chatbot.tracing [number of turns]
# ===========================================================================
if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    if not TURNS_FILE.exists():
        sys.exit(f"No turns logged yet ({TURNS_FILE}).")
    records = [json.loads(line) for line in TURNS_FILE.read_text(encoding="utf-8").splitlines() if line]
    for rec in records[-n:]:
        print(f"\n=== Turn {rec['id']}: {rec['question']}")
        for s in rec["steps"]:
            print(format_step(s))
