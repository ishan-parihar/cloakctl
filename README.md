# cloakctl

**One persistent stealth browser per profile, driven entirely by CLI verbs over `--json` — plus a registry where AI agents save, compose, and compound reusable automations.**

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue) ![License: MIT](https://img.shields.io/badge/License-MIT-green) ![Version](https://img.shields.io/badge/version-0.1.0-informational) ![Tests](https://img.shields.io/badge/tests-304_passing-success)

Cookie injection across contexts is the burn vector, so sessions on one profile must be serialized: the jar lives in the browser, import writes into the *running* browser via CDP, and `open` is single-writer by construction. Agents get verbs, not REST. Plan: [`docs/PLAN.md`](./docs/PLAN.md) · workflows: [`docs/WORKFLOWS-PLAN.md`](./docs/WORKFLOWS-PLAN.md) · the one distinction: [`docs/SKILLS-VS-WF.md`](./docs/SKILLS-VS-WF.md).

## Install

Requires **python ≥ 3.10** and **one Chromium** (`cloakbrowser`, `chromium`, `chrome`, `brave`, or `edge`; override with `CLOAKCTL_BROWSER`). Linux v20 cookie decryption needs a Secret Service keyring.

```bash
git clone https://github.com/ishan-parihar/cloakctl && cd cloakctl
./install.sh            # pipx when present, else an isolated venv
cloakctl doctor         # tolerates a missing browser; install one next
cloakctl profiles create linkedin && cloakctl open linkedin
cloakctl import linkedin brave --domain linkedin.com -y
cloakctl validate linkedin   # logged_in | anonymous | challenged | burn_signature
```

## Proof (real transcript)

```bash
$ cloakctl wf save hello --code 'META = {"version": "1.0.0", "description": "Smoke-test module."}
def run(ctx, inputs):
    return {"hello": "world"}' --json
{"workflow": "hello", "saved": true, "depends_on": []}
$ cloakctl wf run hello --profile demo --json
{"workflow": "hello", "ok": true, "steps": 0, "durationMs": 0,
 "call": {"wf": "hello", "inputs": {}, "version": "1.0.0"},
 "callTree": [], "outputs": {"hello": "world"}}
```

## Skills vs workflows (the one distinction)

**Skills carry the *how-to-think*, workflows carry the *how-to-do*.**

| | skill | workflow (`wf`) |
|---|---|---|
| **What** | agent-owned context + recorded macro | executable automation module |
| **Form** | notes + static argv replay | `run(ctx, inputs)` + `META` |
| **Inputs** | none — replays fixed steps | typed, validated spec |
| **Composes** | no — stops at first failure | yes — `ctx.call()`, cycle + budget fuses |
| **Trust** | untrusted guidance (may go `stale`) | operator-trusted code (same as shell) |

Write a **skill** for reusable context ("this site paginates with a More button"); write a **workflow** when the doing itself compounds (`scrape_list` → `paginate_collect` → site collectors). Promotion is one-way: `skill promote` scaffolds a DRAFT with `TODO(semantic)` markers; the agent hardens refs and saves back with `wf save --code`. Inputs declared `"secret": true` are redacted from every call tree, run document, and log.

## Compounding without filesystem access

```bash
cloakctl wf save paginate_collect --code '...' --inputs '{...}'  # heredoc/string, no files
cloakctl wf show paginate_collect --source     # read back, re-save to iterate (history kept)
cloakctl wf run paginate_collect --profile p --set url=https://... --timeout 60
cloakctl wf runs paginate_collect              # errors, durations, sub-call depth
cloakctl wf runs paginate_collect --output 2026-09-16T12  # spilled large outputs
cloakctl wf rename a b | prune a --keep 20 | rm a   # builtins refuse rm
```

`wf save` refuses cyclic graphs; runs enforce depth + step + wall-clock budgets. Failures are JSON (`rc=1`), never tracebacks.

## Concurrency rules

- **One profile, one writer.** Parallel `wf run` on one profile MUST pass `--new-tab` (owned tabs, closed afterwards); shared-tab parallelism clobbers snapshot refs.
- The registry is race-safe (atomic writes + interprocess locks, capped histories: 200 runs, 50 imports, rotating audit). Proven by `tests/live_matrix7.py`.

## Debugging natively

```bash
cloakctl audit <profile> [--limit 20]   # command trail, names only, secret-safe
cloakctl wf runs <name> [--output <stamp-prefix>]  # history + spilled evidence
cloakctl skill show <name>              # run history + staleness
cloakctl doctor                         # binary, locks, disk, memory, save/promote hints
```

## Commands

```
cloakctl profiles list|create <name>|rm <name> [--force]
cloakctl open <profile> [--headed] [--browser-arg <arg>]... | close <profile>
cloakctl status [<profile>] | attach <profile> | doctor
cloakctl import <profile> brave [--domain d]... [--dry-run] [-y]
cloakctl validate <profile> [--url <probe>] | exec <profile> "<js>" [--tab <id>]
cloakctl navigate <profile> --url <u> | --back | --forward | --reload [--tab <id>]
cloakctl snapshot <profile> [--tab <id>] [--depth N] [--mode ax|text] | diff <profile>
cloakctl act <profile> click|type|clear|focus --ref e3 [--text t] [--button left] [--count N]
cloakctl act <profile> key --key Enter | hover --ref e3 | scroll [--dy 500]
cloakctl act <profile> select --ref e5 --value b | fill --ref e1 --text v | fill --field e1=a
cloakctl act <profile> check|uncheck --ref e2 | drag --ref e3 --dx 100 --dy 0
cloakctl wait <profile> --text <s> | --selector <css> [--timeout 15] [--tab <id>]
cloakctl read <profile> [--tab <id>] [--format markdown|text|links|console] [--selector <css>]
cloakctl grep <profile> <pattern> [--over ax|text] [--limit 30]
cloakctl screenshot <profile> [--full] [--format png] [--out path] [--tab <id>]
cloakctl pdf <profile> [--landscape] [--out path] [--tab <id>]
cloakctl download <profile> --ref e3 --out-dir <dir> | upload <profile> --ref e4 --file <f>...
cloakctl run <profile> "await fetch(...)" [--timeout 30]
cloakctl tabs <profile> list | new [--url <u>] [--background] | close <id>|active | activate <id>
cloakctl windows <profile> list | activate <w> | close <w>
cloakctl groups <profile> list | group <label> <id>... | ungroup <id>... | rename <old> <new>
cloakctl history <profile> [--limit 20] [--query <q>] | audit <profile>
cloakctl session <profile> <label> [--summary <s>] [--category <c>]
cloakctl skill save <name> --description <d> --steps '[{"cmd":[...]}]' [--site <s>] [--notes <n>]
cloakctl skill list | show <name> | search <text> | promote <skill> [--to <wf>]
cloakctl skill run <name> [--timeout 60] | rm <name> | rename <old> <new> | log-run <name>
cloakctl wf list | show <name> [--source] | search <text> | export <name> [--out file]
cloakctl wf save <name> --file mod.py | --code '...' | --file - [--inputs '{...}'] [--depends a,b]
cloakctl wf run <name> --profile <p> [--input '{...}'] [--set k=v]... [--timeout 60] [--new-tab]
cloakctl wf runs <name> [--limit 20] [--output <stamp>] | rename <o> <n> | rm <n> | prune <n>
```

Page text ships in `[UNTRUSTED_PAGE_CONTENT nonce=...]` markers (data, not instructions). Typical loop: `navigate` → `snapshot` → `act --ref` → `diff`. Cookie values are never printed.

## Guarantees

- **Single writer**: `open` re-attaches, never launches twice (pid + start-time locks); `doctor` flags stale locks.
- **No cookie egress**: read-only extraction (WAL-aware copy, temp-dir decrypt); same-context import refuses cold profiles.
- **Detached browser** (setsid); `close` is SIGTERM → SIGKILL with cookie flush + orphan sweep.
- **JSON-always**: every failure is a JSON error + nonzero exit.

## Layout & env

```
~/.cloakctl/
  profiles/<name>/chrome/     the session itself (persistent user-data-dir)
  profiles/<name>/meta.json   created/lastUsed/importHistory (fingerprints only)
  skills/<name>.json          skill docs (history capped at 200)
  workflows/<name>/           module.py + manifest.json (+ runs/<stamp>/ spill)
  runtime/                    lock.<name>.json · audit.<name>.jsonl (rotating) · refs.<name>.<tab>.*
```

| Env | Purpose | Default |
|---|---|---|
| `CLOAKCTL_BROWSER` | browser binary override | auto-detect |
| `CLOAKCTL_HOME` | state dir (per-suite isolation in tests) | `~/.cloakctl` |
| `CLOAKCTL_PROBE_URL` | `validate` target | LinkedIn feed |
| `CLOAKCTL_MIN_MEM_MB` | launch RAM floor | 400 |

## Tests & contributing

```bash
python -m pytest tests/test_core.py tests/test_wf_prod.py   # 58 unit, no browser
for t in live_matrix live_matrix2 live_matrix3 live_matrix4 live_matrix5 live_matrix6 live_matrix7 live_auto; do python tests/$t.py; done
```

Every live suite uses an isolated temp `CLOAKCTL_HOME`, asserts zero strays at teardown, and passes `env=ENV` to every subprocess. PRs: keep the suite green, keep failures JSON. License: [MIT](./LICENSE).
