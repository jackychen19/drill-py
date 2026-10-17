#!/usr/bin/env python3
"""One-time conversion: card(...) definitions in syntax_drill.py -> cards.json.

Each reference solution is run with the same runner the server uses, and its
output is stored as "expected". After this, the app only reads cards.json.

Usage:  python3 convert_cards.py [path/to/syntax_drill.py]
        (defaults to syntax_drill.py next to this script)
"""
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

from server import CARDS_FILE, run_code


def load_cards(path):
    spec = importlib.util.spec_from_file_location("syntax_drill", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # main() is behind __name__ == "__main__"
    return module.CARDS


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("syntax_drill.py")
    if not src.exists():
        sys.exit(f"Can't find {src}. Pass its path: python3 convert_cards.py path/to/syntax_drill.py")

    cards, problems, seen = [], [], set()
    for c in load_cards(src):
        if c["id"] in seen:
            problems.append(f"{c['id']}: duplicate id")
        seen.add(c["id"])
        output, error = run_code(c["setup"], c["solution"])
        if error or not output.strip():
            problems.append(f"{c['id']}: error={error!r} output={output!r}")
        cards.append({
            "id": c["id"],
            "cat": c["cat"],
            "lang": "python",
            "prompt": c["prompt"],
            "setup": c["setup"],
            "solution": c["solution"],
            "expected": output,
        })

    if problems:
        print("Not writing cards.json; these reference solutions are broken:")
        print("\n".join("  " + p for p in problems))
        sys.exit(1)

    CARDS_FILE.write_text(json.dumps(cards, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    counts = ", ".join(f"{cat} {n}" for cat, n in Counter(c["cat"] for c in cards).items())
    print(f"Wrote {len(cards)} cards to {CARDS_FILE.name} ({counts})")


if __name__ == "__main__":
    main()
