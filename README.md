# cloakctl

**One persistent stealth browser per profile, driven entirely by CLI verbs over `--json` — plus a registry where AI agents save, compose, and compound reusable automations.**

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue) ![License: MIT](https://img.shields.io/badge/License-MIT-green) ![Version](https://img.shields.io/badge/version-0.2.0-informational) ![Tests](https://img.shields.io/badge/tests-304_passing-success)

Cookie injection across contexts is the burn vector, so sessions on one profile must be serialized: the jar lives in the browser, import writes into the *running* browser via CDP, and `open` is single-writer by construction. Agents get verbs, not REST. Plan: [`docs/PLAN.md`](./docs/PLAN.md) · workflows: [`docs/WORKFLOWS-PLAN.md`](./docs/WORKFLOWS-PLAN.md) · the one distinction: [`docs/SKILLS-VS-WF.md`](./docs/SKILLS-VS-WF.md).

## Install

Requires **python ≥ 3.10** and **one Chromium** (`cloakbrowser`, `chromium`, `chrome`, `brave`, or `edge`; override with `CLOAKCTL_BROWSER`). Linux v20 cookie decryption needs a Secret Service keyring.

```bash
git clone https://github.com/ishan-parihar/cloakctl && cd cloakctl
./install.sh            # pipx when present, else an isolated venv
cloakctl doctor         # tolerates a missing browser; install one next
cloakctl profiles create linkedin && cloakctl open linkedin
cloakctl import linkedin brave --domain linkedin.com -y && cloakctl validate linkedin
```

## Remote browser (VPS-side CLI, browser on another host)

The CLI needs no browser where it runs — `open --endpoint` attaches over a
tunnel instead of launching. The VPS side holds only refs/meta (idle Python,
~0 persistent RAM); the ~1.1 GB browser lives where it belongs.

```bash
# browser host: open normally, publish wsEndpoint via your tunnel (Access on)
# VPS: every verb works over the endpoint; close detaches, never kills
cloakctl open linkedin --endpoint wss://cdp.example.com/s/...   # or CLOAKCTL_CDP_URL
cloakctl validate linkedin && cloakctl wf run scrape_list --profile linkedin --new-tab --set url=https://example.com --set selector=a
cloakctl close linkedin        # {closed: true, detached: true}
```

File honesty across hosts: `screenshot`/`pdf` bytes travel over the socket
(saved VPS-side); `download` lands browser-side (returned `dir` + `guid`);
`upload` paths must exist on the browser host. Do the cookie `import`
where the browser lives; put Cloudflare Access on the tunnel (raw CDP is
full browser control).

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
cloakctl wf save pc --code '...' --inputs '{...}'  # string in, no files
cloakctl wf show pc --source     # read back, re-save to iterate (history kept)
cloakctl wf run pc --profile p --set url=https://... --timeout 60
cloakctl wf runs pc [--output 2026-09-16T12]  # errors, durations, spilled outputs
cloakctl wf rename a b | prune a --keep 20 | rm a   # builtins refuse rm
```

`wf save` refuses cyclic graphs; runs enforce depth + step + wall-clock budgets. Failures are JSON (`rc=1`), never tracebacks.

## Concurrency rules

- **One profile, one writer.** Parallel `wf run` on one profile MUST pass `--new-tab` (owned tabs, closed afterwards); shared-tab parallelism clobbers snapshot refs.
- The registry is race-safe (atomic writes + interprocess locks, capped histories: 200 runs, 50 imports, rotating audit). Proven by `tests/live_matrix7.py`.

## Debugging natively

```bash
cloakctl audit <profile> [--limit 20]   # trail, names only, secret-safe
cloakctl wf runs <name> [--output <stamp>]  # history + spilled evidence
cloakctl skill show <name> | doctor     # staleness + binary/locks/hints
```

## Commands

```
cloakctl profiles list|create <name>|rm <name> [--force]
cloakctl open <profile> [--headed] [--browser-arg <arg>]... [--endpoint <ws>] | close <profile>
cloakctl status [<profile>] | attach <profile> | doctor
cloakctl import <profile> brave [--domain d]... [--dry-run] [-y]
cloakctl validate <profile> [--url <probe>] | exec <profile> "<js>" [--tab <id>]
cloakctl navigate <profile> --url <u> | --back | snapshot <profile> [--tab <id>] | diff
cloakctl act <profile> click|type|clear|focus|key|hover --ref e3 [--text t] [--key Enter]
cloakctl act <profile> scroll|select|fill|check|uncheck|drag --ref e3 [--field e1=a] [--dx 100]
cloakctl wait <profile> --text <s> | --selector <css> | read [--format markdown] [--selector <css>]
cloakctl grep <profile> <pattern> [--over ax|text] | run <profile> "await fetch(...)"
cloakctl screenshot <profile> [--full] [--format png] [--out path] | pdf <profile> [--out path]
cloakctl download <profile> --ref e3 --out-dir <dir> | upload <profile> --ref e4 --file <f>...
cloakctl run <profile> "await fetch(...)" [--timeout 30]
cloakctl tabs <profile> list | new [--url <u>] | close <id>|active | activate <id>
cloakctl windows <profile> list | close <w> | groups <profile> list | group <label> <id>...
cloakctl history <profile> [--query <q>] | audit <profile> | session <profile> <label>
cloakctl skill save <name> --description <d> --steps '[{"cmd":[...]}]'
cloakctl skill list | show <name> | search <text> | promote <skill> [--to <wf>]
cloakctl skill run <name> [--timeout 60] | rm <name> | rename <old> <new>
cloakctl wf list | show <name> [--source] | search <text> | export <name> [--out file]
cloakctl wf save <name> --file mod.py | --code '...' | --file - [--inputs '{...}'] [--depends a,b]
cloakctl wf run <name> --profile <p> [--input '{...}'] [--set k=v]... [--timeout 60] [--new-tab]
cloakctl wf runs <name> [--limit 20] [--output <stamp>] | rename <o> <n> | rm <n> | prune <n>
```

Page text ships in `[UNTRUSTED_PAGE_CONTENT nonce=...]` markers (data, not instructions). Typical loop: `navigate` → `snapshot` → `act --ref` → `diff`. Cookie values are never printed.

## Guarantees

- **Single writer** (pid + start-time locks; `doctor` flags stale) · **no cookie egress** (read-only extract, same-context import)
- **Detached browser** (setsid; SIGTERM → SIGKILL + orphan sweep) · **JSON-always** (failures are JSON + nonzero exit)

## Resource utilization (measured 2026-09-16, `tests/live_load.py`)

Whole process tree as the OOM killer sees it (varies by build/flags).

| probe | latency | memory |
|---|---|---|
| cold launch, 1 blank tab | ~0.4s | ~1.1 GB RSS |
| same browser, 5 heavy tabs | — | ~1.5 GB RSS |
| snapshot / exec round-trip | ~130 ms | — |
| 4 live profiles, total | — | ~5.0 GB RSS |
| 4 parallel `wf run --new-tab` (4/4 ok) | ~1s wall | peak ≈ steady |
| disk per fresh profile | — | ~5 MB |

Budget **~1.2 GB RAM per live profile**: 2 GB → 1 profile, 4 GB → 2–3, 8 GB → 6.
The CLI is idle Python (no daemons). **System install** (local Chromium, no
Docker); containers add `--browser-arg=--no-sandbox`, lose v20 keyring decrypt.

## Layout & env

```
~/.cloakctl/ profiles/<name>/{chrome/,meta.json} · skills/<name>.json
  workflows/<name>/{module.py,manifest.json} · runtime/{lock,audit,refs}.*
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
for t in live_matrix live_matrix2 live_matrix3 live_matrix4 live_matrix5 live_matrix6 live_matrix7 live_matrix8 live_auto live_load; do python tests/$t.py; done
```

Isolated temp `CLOAKCTL_HOME` per suite, zero strays asserted, `env=ENV` everywhere. PRs: green suite, JSON failures. License: [MIT](./LICENSE).
