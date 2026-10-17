#!/usr/bin/env python3
"""drill_ : local flashcards for drilling Python syntax from memory.

Run:   python3 server.py          then open http://localhost:8000
       python3 server.py 8080     to use another port

Each deck is one JSON file in decks/ (a list of card objects). Progress for
every deck lives in progress.json.

Stdlib only. The server executes code you type, so it binds to 127.0.0.1 and
rejects requests whose Host or Origin isn't this machine (blocks other websites
and DNS-rebinding tricks from reaching /run).
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

try:
    import resource  # POSIX only; used to cap runaway output
except ImportError:  # pragma: no cover - Windows
    resource = None

HOST = "127.0.0.1"
DEFAULT_PORT = 8000
ROOT = Path(__file__).resolve().parent
INDEX_FILE = ROOT / "index.html"
DECKS_DIR = Path(os.environ.get("DRILL_DECKS", ROOT / "decks"))
DEFAULT_DECK = "starter"
PROGRESS_FILE = Path(os.environ.get("DRILL_PROGRESS", ROOT / "progress.json"))

TIMEOUT = 5  # seconds per run
MAX_OUTPUT = 64 * 1024  # bytes of stdout returned to the browser
MAX_FILE = 1024 * 1024  # bytes the child may write before it is killed
MAX_BODY = 256 * 1024
RETIRED = 3  # box 3 = retired
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
DECK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,39}")  # also the file name, so no dots or slashes
PUBLIC_FIELDS = ("id", "cat", "lang", "prompt", "source", "setup")

ERR_MARK = "__DRILL_ERROR__"

# Runs inside the child process. Setup and code arrive as JSON on stdin and are
# compiled separately, so error line numbers refer to your code, not the setup.
HARNESS = r"""
import json, sys
src = json.load(sys.stdin)
ns = {"__name__": "__main__"}
try:
    exec(compile(src["setup"], "<given>", "exec"), ns)
    exec(compile(src["code"], "<your code>", "exec"), ns)
except BaseException as e:
    sys.stdout.flush()
    msg = str(e)
    sys.stderr.write("\n%s%s%s" % (MARK, type(e).__name__, (": " + msg) if msg else ""))
""".replace("MARK", repr(ERR_MARK))


def _limit_output():
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE, MAX_FILE))


def run_code(setup, code):
    """Run setup + code with the real python3 in a subprocess. Return (output, error)."""
    payload = json.dumps({"setup": setup, "code": code}).encode()
    with tempfile.TemporaryDirectory(prefix="drill-") as tmp:
        out_path, err_path = Path(tmp, "out"), Path(tmp, "err")
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            try:
                proc = subprocess.run(
                    [sys.executable, "-I", "-X", "utf8", "-c", HARNESS],
                    input=payload,
                    stdout=out,
                    stderr=err,
                    cwd=tmp,
                    timeout=TIMEOUT,
                    preexec_fn=_limit_output if resource else None,
                )
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                rc = None
        output = _read_capped(out_path)
        stderr = err_path.read_bytes()[-MAX_OUTPUT:].decode("utf-8", "replace")

    if rc is None:
        return output, f"TimeoutError: your code ran longer than {TIMEOUT} seconds (infinite loop?)"
    if "[Errno 27]" in stderr:  # EFBIG: hit the RLIMIT_FSIZE cap
        return output, "OutputLimitError: your code printed more than 1 MB"
    if ERR_MARK in stderr:
        return output, stderr.rsplit(ERR_MARK, 1)[1].strip()
    if rc != 0:
        last = stderr.strip().splitlines()[-1:] or [f"process exited with code {rc}"]
        return output, last[0]
    return output, None


def _read_capped(path):
    with open(path, "rb") as f:
        data = f.read(MAX_OUTPUT + 1)
    text = data[:MAX_OUTPUT].decode("utf-8", "replace").rstrip()
    if len(data) > MAX_OUTPUT:
        text += "\n… (output truncated)"
    return text


def _write_json(path, obj):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def build_card(fields, expected, lang="python", extras=None):
    """A card object in the on-disk key order. Empty optional fields are left out."""
    card = {"id": fields["id"], "cat": fields["cat"], "lang": lang or "python", "prompt": fields["prompt"]}
    if fields.get("source"):
        card["source"] = fields["source"]
    card.update(setup=fields.get("setup", ""), solution=fields["solution"], expected=expected)
    card.update({k: v for k, v in (extras or {}).items() if k not in card and k != "source"})
    return card


class Cards:
    """One deck file, re-read whenever it changes, so new cards need no restart."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.mtime = None
        self.cards = []
        self.index = {}

    def _refresh(self):
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            self.mtime, self.cards, self.index = "missing", [], {}
            return
        if mtime == self.mtime:
            return
        self.mtime = mtime
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("top level must be a list of card objects")
        except ValueError as e:
            print(f"! {self.path.name} is not valid ({e}); keeping the previous {len(self.cards)} cards")
            return

        cards, index = [], {}
        for i, c in enumerate(raw):
            card = self._normalize(i, c, index)
            if card:
                cards.append(card)
                index[card["id"]] = card
        self.cards, self.index = cards, index
        print(f"  loaded {len(cards)} cards from {self.path.name}")

    def _normalize(self, i, c, seen):
        where = f"{self.path.name} card #{i + 1}"
        if not isinstance(c, dict) or not all(isinstance(c.get(k), str) and c.get(k) for k in ("id", "cat", "prompt", "solution")):
            print(f"! {where} skipped: needs non-empty string id, cat, prompt and solution")
            return None
        if c["id"] in seen:
            print(f"! {where} skipped: duplicate id {c['id']!r}")
            return None
        card = {
            "id": c["id"],
            "cat": c["cat"],
            "lang": c.get("lang") or "python",
            "prompt": c["prompt"],
            "source": c.get("source") if isinstance(c.get("source"), str) else "",
            "setup": c.get("setup") or "",
            "solution": c["solution"],
            "expected": c.get("expected"),
        }
        if not isinstance(card["expected"], str):
            # Hand-added card without "expected": compute it once from the solution.
            old = self.index.get(card["id"])
            if old and (old["setup"], old["solution"]) == (card["setup"], card["solution"]):
                card["expected"] = old["expected"]
            elif card["lang"] == "python":
                output, error = run_code(card["setup"], card["solution"])
                if error:
                    print(f"! {where} ({card['id']!r}) skipped: its solution raises {error}")
                    return None
                card["expected"] = output
            else:
                print(f"! {where} ({card['id']!r}) skipped: needs an \"expected\" field")
                return None
        return card

    def public(self):
        with self.lock:
            self._refresh()
            return [{k: c[k] for k in PUBLIC_FIELDS} for c in self.cards]

    def get(self, card_id):
        with self.lock:
            self._refresh()
            return self.index.get(card_id)

    # -- editing from the Deck page ---------------------------------------
    # These rewrite the deck file as a whole, keeping card order and any extra
    # fields a hand-edited card carries. A file that doesn't parse is never
    # overwritten, so a half-finished hand edit can't be clobbered.
    def _read_raw(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except ValueError as e:
            raise ValueError(f"{self.path.name} has a JSON error ({e}). Fix it by hand first.")
        if not isinstance(raw, list):
            raise ValueError(f"{self.path.name} must be a list of card objects. Fix it by hand first.")
        return raw

    def _write_raw(self, raw):
        _write_json(self.path, raw)
        self.mtime = None
        self._refresh()

    def save(self, original_id, fields, expected):
        """Insert or update a card. Returns the stored card; raises ValueError."""
        with self.lock:
            raw = self._read_raw()
            pos = None
            if original_id is not None:
                pos = next((i for i, c in enumerate(raw) if isinstance(c, dict) and c.get("id") == original_id), None)
                if pos is None:
                    raise ValueError(f"card {original_id!r} no longer exists in this deck")
            if fields["id"] != original_id and any(isinstance(c, dict) and c.get("id") == fields["id"] for c in raw):
                raise ValueError(f"id {fields['id']!r} is already used by another card in this deck")
            old = raw[pos] if pos is not None else {}
            card = build_card(fields, expected, old.get("lang"), old)
            if pos is None:
                raw.append(card)
            else:
                raw[pos] = card
            self._write_raw(raw)
            return self.index.get(card["id"])

    def add_copies(self, cards):
        """Append copies of cards from another deck. Clashing ids get a -2, -3… suffix.
        Returns {source id: new id}."""
        with self.lock:
            raw = self._read_raw()
            taken = {c.get("id") for c in raw if isinstance(c, dict)}
            placed = {}
            for c in cards:
                new_id, n = c["id"], 2
                while new_id in taken:
                    new_id, n = f"{c['id']}-{n}", n + 1
                taken.add(new_id)
                placed[c["id"]] = new_id
                raw.append(build_card({**c, "id": new_id}, c["expected"], c["lang"]))
            self._write_raw(raw)
            return placed

    def delete(self, card_id):
        with self.lock:
            raw = self._read_raw()
            kept = [c for c in raw if not (isinstance(c, dict) and c.get("id") == card_id)]
            if len(kept) == len(raw):
                raise ValueError(f"card {card_id!r} not found")
            self._write_raw(kept)


class Decks:
    """The decks/ folder: one Cards per <name>.json."""

    def __init__(self, folder):
        self.dir = folder
        self.lock = threading.Lock()
        self.open = {}

    def setup(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        if not self.names():
            _write_json(self.dir / f"{DEFAULT_DECK}.json", [])
            print(f"  created an empty {DEFAULT_DECK} deck in {self.dir.name}/")

    def names(self):
        return sorted((p.stem for p in self.dir.glob("*.json") if DECK_RE.fullmatch(p.stem)), key=str.lower)

    def _taken(self, name, ignore=None):
        return any(n.lower() == name.lower() and n != ignore for n in self.names())

    def get(self, name):
        if not isinstance(name, str) or name not in self.names():
            return None
        with self.lock:
            if name not in self.open:
                self.open[name] = Cards(self.dir / f"{name}.json")
            return self.open[name]

    def summary(self):
        return [{"name": n, "count": len(self.get(n).public())} for n in self.names()]

    @staticmethod
    def clean_name(name):
        name = name.strip() if isinstance(name, str) else ""
        if not DECK_RE.fullmatch(name):
            raise ValueError("Deck names are 1-40 letters, digits, spaces, - or _, starting with a letter or digit.")
        return name

    def create(self, name):
        name = self.clean_name(name)
        with self.lock:
            if self._taken(name):
                raise ValueError(f"There's already a deck called {name!r}.")
            _write_json(self.dir / f"{name}.json", [])
        return name

    def rename(self, old, new):
        new = self.clean_name(new)
        with self.lock:
            if old not in self.names():
                raise ValueError(f"No deck called {old!r}.")
            if self._taken(new, ignore=old):
                raise ValueError(f"There's already a deck called {new!r}.")
            os.replace(self.dir / f"{old}.json", self.dir / f"{new}.json")
            self.open.pop(old, None)
            self.open.pop(new, None)
        return new

    def delete(self, name):
        with self.lock:
            names = self.names()
            if name not in names:
                raise ValueError(f"No deck called {name!r}.")
            if len(names) == 1:
                raise ValueError("That's your only deck. Create another one before deleting it.")
            (self.dir / f"{name}.json").unlink()
            self.open.pop(name, None)


class Progress:
    """progress.json: {"version": 2, "active": deck, "decks": {deck: {card id: entry}}}.
    An entry is {"box", "misses", "passes", "last"}."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.active = None
        self.needs_save = False
        self.migrated = False  # loaded a version-1 file
        self.decks = self._load()

    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.needs_save = True
            return {}
        except (OSError, ValueError) as e:
            bad = self.path.with_suffix(".json.bad")
            print(f"! {self.path.name} unreadable ({e}); moved to {bad.name}, starting fresh")
            os.replace(self.path, bad)
            return {}
        if not isinstance(raw, dict):
            return {}
        if "decks" in raw:
            decks = raw["decks"] if isinstance(raw["decks"], dict) else {}
            self.active = raw.get("active") if isinstance(raw.get("active"), str) else None
        else:
            # Version 1 had a single deck; it became the starter deck.
            decks = {DEFAULT_DECK: raw.get("cards", {})}
            self.needs_save = self.migrated = True
        return {name: self._clean(cards) for name, cards in decks.items() if isinstance(cards, dict)}

    @staticmethod
    def _clean(cards):
        clean = {}
        for cid, v in cards.items():
            if isinstance(v, dict):
                clean[cid] = {
                    "box": min(max(int(v.get("box", 0)), 0), RETIRED),
                    "misses": int(v.get("misses", 0)),
                    "passes": int(v.get("passes", 0)),
                    "last": v.get("last"),
                }
        return clean

    def save(self):
        _write_json(self.path, {"version": 2, "active": self.active, "decks": self.decks})

    def _entry(self, deck, cid):
        return self.decks.setdefault(deck, {}).setdefault(cid, {"box": 0, "misses": 0, "passes": 0, "last": None})

    def record_run(self, deck, cid, passed, first_try):
        with self.lock:
            e = self._entry(deck, cid)
            if passed:
                e["passes"] += 1
                if first_try:
                    e["box"] = min(e["box"] + 1, RETIRED)
            else:
                e["misses"] += 1
            e["last"] = int(time.time())
            self.save()
            return e["box"]

    def reveal(self, deck, cid):
        with self.lock:
            e = self._entry(deck, cid)
            e["box"] = 0
            e["last"] = int(time.time())
            self.save()
            return 0

    def box(self, deck, cid):
        with self.lock:
            return self.decks.get(deck, {}).get(cid, {}).get("box", 0)

    def snapshot(self, deck):
        with self.lock:
            return {cid: {"box": e["box"], "misses": e["misses"]} for cid, e in self.decks.get(deck, {}).items()}

    def reset(self, deck):
        with self.lock:
            self.decks.pop(deck, None)
            self.save()

    def rename_card(self, deck, old, new):
        with self.lock:
            cards = self.decks.get(deck, {})
            if old in cards:
                cards[new] = cards.pop(old)
                self.save()

    def forget_card(self, deck, cid):
        with self.lock:
            if self.decks.get(deck, {}).pop(cid, None) is not None:
                self.save()

    def rename_deck(self, old, new):
        with self.lock:
            if old in self.decks:
                self.decks[new] = self.decks.pop(old)
            if self.active == old:
                self.active = new
            self.save()

    def drop_deck(self, name):
        with self.lock:
            self.decks.pop(name, None)
            if self.active == name:
                self.active = None
            self.save()

    def set_active(self, name):
        with self.lock:
            if self.active != name:
                self.active = name
                self.save()


DECKS = Decks(DECKS_DIR)
PROGRESS = Progress(PROGRESS_FILE)


class Handler(BaseHTTPRequestHandler):
    server_version = "drill/1"
    allowed_hosts = set()

    # -- plumbing ----------------------------------------------------------
    def _send(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def _local_only(self, post):
        if self.headers.get("Host", "") not in self.allowed_hosts:
            self._json(403, {"error": "forbidden host"})
            return False
        if post:
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {f"http://{h}" for h in self.allowed_hosts}:
                self._json(403, {"error": "forbidden origin"})
                return False
            # application/json forces a CORS preflight, which we never approve.
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                self._json(415, {"error": "expected application/json"})
                return False
        return True

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            self._json(413 if length > MAX_BODY else 400, {"error": "bad request body"})
            return None
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            self._json(400, {"error": "invalid JSON"})
            return None
        if not isinstance(body, dict):
            self._json(400, {"error": "expected a JSON object"})
            return None
        return body

    def log_message(self, fmt, *args):
        if "/api/state" not in (args[0] if args else ""):
            sys.stderr.write("  %s\n" % (fmt % args))

    def _deck(self, name, fallback=False):
        """Resolve a deck name to (name, Cards). No name means the active deck.
        With fallback, an unknown name also falls back instead of failing."""
        names = DECKS.names()
        if name in (None, "") or (fallback and name not in names):
            name = PROGRESS.active if PROGRESS.active in names else (DEFAULT_DECK if DEFAULT_DECK in names else (names or [None])[0])
        cards = DECKS.get(name)
        if not cards:
            self._json(404, {"error": f"no deck called {name!r}"})
        return name, cards

    def _card(self, body):
        deck, cards = self._deck(body.get("deck"))
        if not cards:
            return deck, None
        card = cards.get(body.get("cardId")) if isinstance(body.get("cardId"), str) else None
        if not card:
            self._json(404, {"error": "unknown cardId"})
        return deck, card

    # -- routes ------------------------------------------------------------
    def do_GET(self):
        if not self._local_only(post=False):
            return
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path in ("/", "/index.html"):
            self._send(200, INDEX_FILE.read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/api/state":
            deck, cards = self._deck(query.get("deck"), fallback=True)
            if cards:
                self._json(200, {
                    "deck": deck,
                    "decks": DECKS.summary(),
                    "cards": cards.public(),
                    "progress": PROGRESS.snapshot(deck),
                })
        elif url.path.startswith("/api/cards/"):
            # Full card including solution, for the Deck page's edit form.
            deck, cards = self._deck(query.get("deck"))
            if not cards:
                return
            card = cards.get(unquote(url.path[len("/api/cards/"):]))
            if card:
                self._json(200, {k: card[k] for k in (*PUBLIC_FIELDS, "solution", "expected")})
            else:
                self._json(404, {"error": "unknown card"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._local_only(post=True):
            return
        route = {
            "/run": self._run,
            "/api/reveal": self._reveal,
            "/api/reset": self._reset,
            "/api/cards/test": self._test_card,
            "/api/cards/save": self._save_card,
            "/api/cards/delete": self._delete_card,
            "/api/cards/copy": self._copy_cards,
            "/api/decks/create": self._create_deck,
            "/api/decks/rename": self._rename_deck,
            "/api/decks/delete": self._delete_deck,
            "/api/decks/select": self._select_deck,
        }.get(self.path)
        if not route:
            return self._json(404, {"error": "not found"})
        body = self._body()
        if body is not None:
            route(body)

    def _run(self, body):
        code = body.get("code")
        if not isinstance(code, str):
            return self._json(400, {"error": "code must be a string"})
        deck, card = self._card(body)
        if not card:
            return
        if card["lang"] != "python":
            return self._json(400, {"error": f"lang {card['lang']!r} is not supported yet"})
        output, error = run_code(card["setup"], code)
        # Expected output always comes from the server's copy of the card.
        passed = error is None and output.strip() == card["expected"].strip()
        box = PROGRESS.record_run(deck, card["id"], passed, body.get("firstTry") is True)
        self._json(200, {"output": output, "error": error, "passed": passed, "expected": card["expected"], "box": box})

    def _reveal(self, body):
        deck, card = self._card(body)
        if not card:
            return
        # reset=false is for peeking after a pass, when the card is already scored.
        if body.get("reset", True) is not False:
            box = PROGRESS.reveal(deck, card["id"])
        else:
            box = PROGRESS.box(deck, card["id"])
        self._json(200, {"solution": card["solution"], "expected": card["expected"], "box": box})

    def _reset(self, body):
        deck, cards = self._deck(body.get("deck"))
        if cards:
            PROGRESS.reset(deck)
            self._json(200, {"ok": True})

    def _test_card(self, body):
        setup, solution = body.get("setup", ""), body.get("solution")
        if not isinstance(setup, str) or not isinstance(solution, str):
            return self._json(400, {"error": "setup and solution must be strings"})
        output, error = run_code(setup, solution)
        self._json(200, {"output": output, "error": error})

    def _save_card(self, body):
        original_id = body.get("originalId")
        raw = body.get("card")
        if (original_id is not None and not isinstance(original_id, str)) or not isinstance(raw, dict):
            return self._json(400, {"error": "expected {deck, originalId, card}"})
        deck, cards = self._deck(body.get("deck"))
        if not cards:
            return
        fields = {k: raw.get(k, "") for k in ("id", "cat", "prompt", "source", "setup", "solution")}
        if not all(isinstance(v, str) for v in fields.values()):
            return self._json(400, {"error": "card fields must be strings"})
        fields["id"] = fields["id"].strip()
        fields["cat"] = fields["cat"].strip().lower()
        fields["prompt"] = fields["prompt"].strip()
        fields["source"] = fields["source"].strip()
        fields["setup"] = fields["setup"].strip("\n")
        fields["solution"] = fields["solution"].strip("\n")
        for k in ("id", "cat", "prompt", "solution"):
            if not fields[k].strip():
                return self._json(422, {"error": f"{k} is required"})
        if not ID_RE.fullmatch(fields["id"]):
            return self._json(422, {"error": "id can only use letters, digits, - and _"})
        if not ID_RE.fullmatch(fields["cat"]):
            return self._json(422, {"error": "category can only use letters, digits, - and _"})

        # The expected output is always computed here, never taken from the client.
        output, error = run_code(fields["setup"], fields["solution"])
        if error:
            return self._json(422, {"error": "The solution raised an exception.", "output": output, "runError": error})
        if not output.strip():
            return self._json(422, {"error": "The solution doesn't print anything. Cards are checked by printed output.", "output": output})
        try:
            card = cards.save(original_id, fields, output)
        except ValueError as e:
            return self._json(409, {"error": str(e)})
        if original_id and original_id != card["id"]:
            PROGRESS.rename_card(deck, original_id, card["id"])
        self._json(200, {"card": {k: card[k] for k in (*PUBLIC_FIELDS, "solution", "expected")}})

    def _delete_card(self, body):
        card_id = body.get("id")
        if not isinstance(card_id, str):
            return self._json(400, {"error": "id must be a string"})
        deck, cards = self._deck(body.get("deck"))
        if not cards:
            return
        try:
            cards.delete(card_id)
        except ValueError as e:
            return self._json(404, {"error": str(e)})
        PROGRESS.forget_card(deck, card_id)
        self._json(200, {"ok": True})

    def _copy_cards(self, body):
        ids = body.get("ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            return self._json(400, {"error": "ids must be a non-empty list of card ids"})
        src_name, src = self._deck(body.get("fromDeck"))
        if not src:
            return
        dst_name, dst = self._deck(body.get("toDeck"))
        if not dst:
            return
        if src_name == dst_name:
            return self._json(422, {"error": "Pick a different deck to copy into."})
        picked = [src.get(i) for i in ids]
        if not all(picked):
            return self._json(404, {"error": "some of those cards no longer exist"})
        try:
            placed = dst.add_copies(picked)
        except ValueError as e:
            return self._json(409, {"error": str(e)})
        self._json(200, {"copied": placed})

    def _create_deck(self, body):
        try:
            name = DECKS.create(body.get("name"))
        except ValueError as e:
            return self._json(422, {"error": str(e)})
        PROGRESS.set_active(name)
        self._json(200, {"name": name})

    def _rename_deck(self, body):
        old = body.get("name")
        try:
            new = DECKS.rename(old, body.get("newName"))
        except ValueError as e:
            return self._json(422, {"error": str(e)})
        PROGRESS.rename_deck(old, new)
        self._json(200, {"name": new})

    def _delete_deck(self, body):
        name = body.get("name")
        try:
            DECKS.delete(name)
        except ValueError as e:
            return self._json(422, {"error": str(e)})
        PROGRESS.drop_deck(name)
        self._json(200, {"ok": True})

    def _select_deck(self, body):
        deck, cards = self._deck(body.get("name"))
        if cards:
            PROGRESS.set_active(deck)
            self._json(200, {"name": deck})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    DECKS.setup()
    if PROGRESS.needs_save:
        PROGRESS.save()  # create it, or upgrade a version-1 file in place
    if PROGRESS.migrated:
        print(f"  moved your existing progress into the {DEFAULT_DECK} deck")
    httpd = ThreadingHTTPServer((HOST, port), Handler)
    Handler.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    DECKS.summary()  # load + report each deck at startup
    print(f"drill_ running at http://localhost:{port}  (bound to {HOST} only; Ctrl+C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
