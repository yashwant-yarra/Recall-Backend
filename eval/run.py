"""Run eval/questions.jsonl against a running API; print retrieval rank and answer checks."""
import json
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter

API = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ABSTAIN = ("i don't know based on your documents",)  # the exact refusal sentence in ask.py SYSTEM


def search_pages(question: str) -> list[int]:
    with urllib.request.urlopen(f"{API}/api/v1/search?q={urllib.parse.quote(question)}") as r:
        return [hit["page"] for hit in json.load(r)["results"]]


def ask(question: str) -> str:
    req = urllib.request.Request(f"{API}/api/v1/ask", data=json.dumps({"question": question}).encode(),
                                 headers={"Content-Type": "application/json"})
    tokens, event = [], None
    with urllib.request.urlopen(req) as r:
        for raw in r:
            line = raw.decode().rstrip("\n")
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and event == "token":
                tokens.append(json.loads(line[6:]))
            elif line.startswith("data: ") and event == "error":
                raise RuntimeError(json.loads(line[6:]))
    return "".join(tokens)


questions = [json.loads(l) for l in open("eval/questions.jsonl", encoding="utf-8") if l.strip()]
stats = Counter()
for q in questions:
    pages = search_pages(q["question"])
    rank = next((i for i, p in enumerate(pages, 1) if p in q["pages"]), None)
    answer = ask(q["question"])
    text = answer.lower().replace("\u2019", "'")  # curly apostrophe -> straight
    text = re.sub(r"\[[\d,\s]+\]", "", text)  # drop [n] / [1, 2] citations before matching
    passed = (any(a in text for a in ABSTAIN) if q["expect"] is None
              else re.search(rf"\b{re.escape(q['expect'].lower())}\b", text) is not None)
    t = q["type"]
    stats[t, "n"] += 1
    stats[t, "hit@1"] += rank == 1
    stats[t, "hit@3"] += rank is not None and rank <= 3
    stats[t, "answer"] += passed
    print(f"#{q['id']:>2} {t:<12} rank={rank or '-':<2} answer={'Y' if passed else 'N'}  {answer[:90]!r}")

print()
for t in sorted({t for t, _ in stats}):
    n = stats[t, "n"]
    print(f"{t:<12} n={n}  hit@1 {stats[t, 'hit@1']}/{n}  hit@3 {stats[t, 'hit@3']}/{n}  answer {stats[t, 'answer']}/{n}")
