# drill_

A small local flashcard app for drilling Python syntax from memory. You type the code, it runs with your real `python3`, and your output is compared to the expected output.

## Run it

```sh
python3 server.py
```

Then open http://localhost:8000. You only need Python 3 (stdlib only, no installs). To use a different port, run `python3 server.py 8080`.

The server runs whatever code you type, so it only listens on `127.0.0.1`. It also rejects requests whose `Host` or `Origin` isn't localhost, so other websites open in your browser can't reach it.

## Using it

- **Run**: `⌘ Enter` (macOS) or `Ctrl Enter`. After a pass, the same shortcut (or `Enter` on the focused button) goes to the next card.
- **Editor**: `Tab` inserts 4 spaces, `Shift+Tab` removes an indent level, and `Enter` keeps the current indentation. Select several lines to indent or dedent them together. `Esc` leaves the editor so you can tab to the buttons. There's no autocomplete, on purpose.
- **Scoring** (Leitner-style boxes 0 to 3; box 3 = retired):
  - Pass on the first run: the card moves up one box.
  - Pass after a failed run: the box stays the same.
  - Show solution: the card goes back to box 0. Peeking after you've already passed is free.
- **Picking**: lower boxes come up more often (odds 6 : 3 : 1 for boxes 0, 1, 2). Within a box it picks at random, never the same card twice in a row. Retired cards stop appearing.
- **Progress** page: a per-category breakdown, **Drill the weakest 10**, **Review retired**, and **Reset progress**.

Each run gets 5 seconds and 1 MB of output. Exceptions show as `Type: message`, the same as Python prints them.

## Files

| File | What it is |
| --- | --- |
| `server.py` | HTTP server and code runner |
| `index.html` | The whole UI (vanilla HTML/CSS/JS) |
| `cards.json` | The deck |
| `progress.json` | Your boxes, created automatically (gitignored) |
| `convert_cards.py` | One-time importer from `syntax_drill.py` |

## Add a card

Append an object to the list in `cards.json`:

```json
{
  "id": "dict-pop-default",
  "cat": "dict",
  "prompt": "Remove `\"z\"` from d without a KeyError if it's missing, then print d.",
  "setup": "d = {\"a\": 1}",
  "solution": "d.pop(\"z\", None)\nprint(d)"
}
```

- `id`, `cat`, `prompt` and `solution` are required. `setup` is optional; it runs before your code and is shown as **Given**.
- `expected` is optional. If you leave it out, the server runs `solution` once and uses what it prints.
- `lang` defaults to `"python"`.
- Wrap text in backticks in `prompt` to show it as code.
- `cat` is usually one of `dict`, `list`, `set`, `string`, `misc`. A new category gets its own filter chip automatically.

You don't need to restart the server: it reloads `cards.json` whenever the file changes, and the new card shows up the next time a card is picked (or when you reload the page). If the JSON is invalid, the server keeps the previous deck and prints the error in the terminal.

## Re-importing from syntax_drill.py

`cards.json` was generated once with:

```sh
python3 convert_cards.py ../syntax_drill.py
```

This runs every reference solution to record its expected output, and refuses to write the file if any of them fail. Running it again overwrites `cards.json`, including any cards you added by hand.
