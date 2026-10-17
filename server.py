#!/usr/bin/env python3
"""drill_ : local flashcards for drilling Python syntax from memory.

Run:   python3 server.py          then open http://localhost:8000
       python3 server.py 8080     to use another port

Stdlib only. The server executes code you type, so it binds to 127.0.0.1 and
rejects requests whose Host or Origin isn't this machine (blocks other websites
and DNS-rebinding tricks from reaching /run).
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import resource  # POSIX only; used to cap runaway output
except ImportError:  # pragma: no cover - Windows
    resource = None

HOST = "127.0.0.1"
DEFAULT_PORT = 8000
ROOT = Path(__file__).resolve().parent
INDEX_FILE = ROOT / "index.html"
CARDS_FILE = ROOT / "cards.json"
PROGRESS_FILE = Path(os.environ.get("DRILL_PROGRESS", ROOT / "progress.json"))

TIMEOUT = 5  # seconds per run
MAX_OUTPUT = 64 * 1024  # bytes of stdout returned to the browser
MAX_FILE = 1024 * 1024  # bytes the child may write before it is killed
MAX_BODY = 256 * 1024
RETIRED = 3  # box 3 = retired

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


class Cards:
    """cards.json, re-read whenever the file changes, so new cards need no restart."""

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
            if self.mtime != "missing":
                print(f"! {self.path.name} not found. Run: python3 convert_cards.py path/to/syntax_drill.py")
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
        if not isinstance(c, dict) or not all(isinstance(c.get(k), str) and c.get(k) for k in ("id", "cat", "prompt", "solution")):
            print(f"! card #{i + 1} skipped: needs non-empty string id, cat, prompt and solution")
            return None
        if c["id"] in seen:
            print(f"! card #{i + 1} skipped: duplicate id {c['id']!r}")
            return None
        card = {
            "id": c["id"],
            "cat": c["cat"],
            "lang": c.get("lang") or "python",
            "prompt": c["prompt"],
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
                    print(f"! card {card['id']!r} skipped: its solution raises {error}")
                    return None
                card["expected"] = output
            else:
                print(f"! card {card['id']!r} skipped: needs an \"expected\" field")
                return None
        return card

    def public(self):
        with self.lock:
            self._refresh()
            return [{k: c[k] for k in ("id", "cat", "lang", "prompt", "setup")} for c in self.cards]

    def get(self, card_id):
        with self.lock:
            self._refresh()
            return self.index.get(card_id)


class Progress:
    """progress.json: {"cards": {id: {"box", "misses", "passes", "last"}}}."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.cards = self._load()

    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            bad = self.path.with_suffix(".json.bad")
            print(f"! {self.path.name} unreadable ({e}); moved to {bad.name}, starting fresh")
            os.replace(self.path, bad)
            return {}
        cards = raw.get("cards", {}) if isinstance(raw, dict) else {}
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
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"version": 1, "cards": self.cards}, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def _entry(self, cid):
        return self.cards.setdefault(cid, {"box": 0, "misses": 0, "passes": 0, "last": None})

    def record_run(self, cid, passed, first_try):
        with self.lock:
            e = self._entry(cid)
            if passed:
                e["passes"] += 1
                if first_try:
                    e["box"] = min(e["box"] + 1, RETIRED)
            else:
                e["misses"] += 1
            e["last"] = int(time.time())
            self.save()
            return e["box"]

    def reveal(self, cid):
        with self.lock:
            e = self._entry(cid)
            e["box"] = 0
            e["last"] = int(time.time())
            self.save()
            return 0

    def box(self, cid):
        with self.lock:
            return self.cards.get(cid, {}).get("box", 0)

    def reset(self):
        with self.lock:
            self.cards = {}
            self.save()

    def snapshot(self):
        with self.lock:
            return {cid: {"box": e["box"], "misses": e["misses"]} for cid, e in self.cards.items()}


CARDS = Cards(CARDS_FILE)
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

    # -- routes ------------------------------------------------------------
    def do_GET(self):
        if not self._local_only(post=False):
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, INDEX_FILE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._json(200, {"cards": CARDS.public(), "progress": PROGRESS.snapshot()})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._local_only(post=True):
            return
        route = {"/run": self._run, "/api/reveal": self._reveal, "/api/reset": self._reset}.get(self.path)
        if not route:
            return self._json(404, {"error": "not found"})
        body = self._body()
        if body is not None:
            route(body)

    def _card(self, body):
        card = CARDS.get(body.get("cardId")) if isinstance(body.get("cardId"), str) else None
        if not card:
            self._json(404, {"error": "unknown cardId"})
        return card

    def _run(self, body):
        code = body.get("code")
        if not isinstance(code, str):
            return self._json(400, {"error": "code must be a string"})
        card = self._card(body)
        if not card:
            return
        if card["lang"] != "python":
            return self._json(400, {"error": f"lang {card['lang']!r} is not supported yet"})
        output, error = run_code(card["setup"], code)
        # Expected output always comes from the server's copy of the card.
        passed = error is None and output.strip() == card["expected"].strip()
        box = PROGRESS.record_run(card["id"], passed, body.get("firstTry") is True)
        self._json(200, {"output": output, "error": error, "passed": passed, "expected": card["expected"], "box": box})

    def _reveal(self, body):
        card = self._card(body)
        if not card:
            return
        # reset=false is for peeking after a pass, when the card is already scored.
        box = PROGRESS.reveal(card["id"]) if body.get("reset", True) is not False else PROGRESS.box(card["id"])
        self._json(200, {"solution": card["solution"], "expected": card["expected"], "box": box})

    def _reset(self, body):
        PROGRESS.reset()
        self._json(200, {"ok": True})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    if not PROGRESS_FILE.exists():
        PROGRESS.save()
    httpd = ThreadingHTTPServer((HOST, port), Handler)
    Handler.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    CARDS.public()  # load + report card count at startup
    print(f"drill_ running at http://localhost:{port}  (bound to {HOST} only; Ctrl+C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
