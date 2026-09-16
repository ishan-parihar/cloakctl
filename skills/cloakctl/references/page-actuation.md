# Page actuation — verbs, refs, and the loop

All verbs default to the active tab (tracked per profile). `--tab <id>`
targets another tab. Every command prints `--json`.

## The loop

Typical multi-step flow — each pass re-reads the page, so refs stay valid:

1. `navigate <profile> --url <u>` (or `--back` / `--forward` / `--reload`)
2. `snapshot <profile>` — renders the accessibility tree as an indented
   ref tree; interactive elements get refs `e1, e2, ...`
3. `act <profile> click --ref e3` — one verb per interaction
4. `diff <profile>` — unified diff since the last snapshot (did the click
   do what you expected?)
5. `wait <profile> --text "..." | --selector "css"` when a load is async

Refs persist per profile+tab in `runtime/`, so a later `act` invocation
resolves them. If a ref fails after navigation, re-run `snapshot`.

## Act verbs

```
click  --ref e3 [--button left] [--count N]     # falls back to el.click() if off-viewport
type   --ref e1 --text "hello"                  # focus + insertText (IME-safe)
clear  --ref e1                                 # focus + select + backspace
focus  --ref e1
key    --key Enter                              # rawKeyDown/keyUp with virtual codes
hover  --ref e3 | --x 50 --y 60
scroll [--ref e3] [--dx 0] [--dy 500]           # ref = scrollIntoView(center)
select --ref e5 --value b                       # fires input+change events
fill   --ref e1 --text v | fill --field e1=a --field e2=b   # batch, reports filled/failed
check  --ref e2 | uncheck --ref e2              # idempotent (clicks only if state differs)
drag   --ref e3 --dx 100 --dy 0 | --x 50 --y 60 --dx 30 --dy 40
```

Coordinates (`--x/--y`) work where refs don't (canvas, shadow DOM).

## Read + grep

```
read <profile> [--format markdown|text|links|console] [--selector <css>]
grep <profile> <pattern> [--over ax|text] [--limit 30]
```

- `markdown` walks headings/links/lists into a compact doc (60 KB cap)
- `links` returns up to 300 `{text, href}` pairs
- `console` buffers Log.entryAdded + Runtime.exceptionThrown and drains errors
- `grep --over ax` searches the rendered accessibility tree (matches what a
  screen reader sees), `--over text` the raw innerText

## Capture + transfer

```
screenshot <profile> [--full] [--format png|jpeg] [--quality N] [--out path]
pdf        <profile> [--landscape] [--background] [--out path]
download   <profile> --ref e3 --out-dir <dir>
upload     <profile> --ref e4 --selector "#file" --file <path>...
run        <profile> "await fetch(...)" [--timeout 30]
```

- `run` evaluates JS in the active tab; a single expression containing
  `await` runs as an async IIFE. JS exceptions exit 1 with the error text.
- `download` polls the dest dir for the new file. On remote profiles it
  confirms via `Browser.downloadProgress` events and lands browser-side
  (returns `dir` + `guid`) — see remote-vps.md.

## Tabs, windows, groups

```
tabs <profile> list | new [--url <u>] [--background] | close <id>|active | activate <id>
windows <profile> list | activate <w> | close <w>
groups <profile> list | group <label> <id>... | ungroup <id>... | rename <old> <new>
```

- `active` is the last-focused tab, tracked per profile — a fresh CDP
  attach marks many tabs attached, so this alias is what keeps multi-step
  flows on the same tab.
- `tabs list` includes the tracked tab (`tracked: true`).
- `groups list` self-heals: labels pointing at dead targetIds are dropped
  on read, so stale labels never accumulate across long-lived profiles.
- Closing a window cleans its group labels.

## History + session labels

```
history <profile> [--limit 20] [--query <q>]    # navigation history under chrome/Default
audit   <profile> [--limit 20]                  # command trail (names + rc + ms, secret-safe)
session <profile> <label> [--summary <s>] [--category <c>]
```

Session labels persist per profile — resume context across runs without
re-deriving it from history.
