# cloakctl

**One persistent stealth browser per profile, driven by CLI verbs over `--json` — with a registry where AI agents save, compose, and compound reusable automations.**

![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![Version](https://img.shields.io/badge/version-0.3.0-informational) ![License](https://img.shields.io/badge/license-MIT-green) ![Engine](https://img.shields.io/badge/default%20engine-obscura-6E4B9E) ![Tests](https://img.shields.io/badge/tests-83%20unit%20%2B%2029%20%2B%2014%20live-success)

Sessions on one profile must be serialized: cookie injection across contexts is the burn vector, the jar lives in the browser, and `open` is single-writer by construction. Agents get verbs, not REST — every failure is a JSON document with a nonzero exit, never a traceback.

Two engines, one CLI:

| | [obscura](https://github.com/h4ckf0r0day/obscura) (default) | cloakbrowser (opt-in Chromium) |
|---|---|---|
| Install | downloaded by `./install.sh` | bring your own (`chromium`, `chrome`, `brave`, `edge`) |
| RAM per live profile | ~65 MB | ~1.2 GB |
| Cold launch | ~0.25 s | ~0.4 s |
| Downloads / History | fail fast with a fix (see below) | full CDP support |
| Multi-tab | one page per profile (use profiles) | real tabs |

## Install

Requires **python ≥ 3.10**.

```bash
git clone https://github.com/ishan-parihar/cloakctl && cd cloakctl
./install.sh            # cloakctl + obscura binary (pipx when present, else an isolated venv)
cloakctl doctor         # engines, locks, memory, evolution hints
skills/cloakctl/scripts/smoke.sh   # 11-check end-to-end on an isolated state dir
```

Opt out of the obscura download with `./install.sh --engine cloakbrowser` (needs a Chromium-family browser on PATH; override the binary with `CLOAKCTL_BROWSER`).

## MCP server

The same operations are exposed as MCP tools over stdio — install gives you
both binaries:

```json
{"mcpServers": {"cloakctl": {"command": "cloakctl-mcp", "args": []}}}
```

25 tools named `cloakctl_{verb}` (`cloakctl_open`, `cloakctl_snapshot`,
`cloakctl_act`, `cloakctl_wf_run`, …). Core failures arrive as tool errors
with an `error:` line plus a `help:` line naming the fixing command — agents
self-correct without a retry ladder. The MCP server is the right default for
harnesses that speak MCP; the CLI is right for shell loops and pipes.

### Agent-harness integrations

`install.sh` detects installed harnesses and wires cloakctl in (best-effort,
never fatal). Manual setup for fresh installs:

| Harness | Command |
|---|---|
| **hermes-agent** | `./install.sh` auto-syncs `integrations/hermes/browser-cloakctl/` → `~/.hermes/hermes-agent/plugins/browser/cloakctl/`; or: `cp -r integrations/hermes/browser-cloakctl ~/.hermes/hermes-agent/plugins/browser/cloakctl` then set `browser.cloud_provider: cloakctl` in config.yaml |
| **opencode** | merge `{"mcp": {"cloakctl": {"type": "local", "command": ["cloakctl-mcp"]}}}` into `~/.config/opencode/opencode.json` |
| **omp** | add to `~/.omp/agent.toml`: `[mcpServers.cloakctl]` + `command = "cloakctl-mcp"` |
| **codex** | add to `~/.codex/config.toml`: `[mcp_servers.cloakctl]` + `command = "cloakctl-mcp"` |
| **claude code** | `claude mcp add cloakctl -- cloakctl-mcp` |

The hermes plugin is the deep integration: it registers a local
`BrowserProvider` (persistent per-profile browser, cookie persistence across
agent turns). Note it drives profiles with `--engine cloakbrowser` because
obscura's per-connection CDP cannot be shared with hermes' browser driver.
All other harnesses consume the MCP server.

## Quick start

```bash
cloakctl profiles create linkedin
cloakctl open linkedin                      # idempotent: re-attaches, never launches twice
cloakctl navigate linkedin --url https://example.com
cloakctl snapshot linkedin                  # refs e1,e2,... map to interactive elements
cloakctl act linkedin click --ref e3
cloakctl close linkedin
```

Every verb works over `--json` for agents (`--toon` for token-efficient TOON
output; bare `cloakctl` shows a live home view); humans can drop the flag.

## The compounding loop

Skills carry the *how-to-think*; workflows carry the *how-to-do*.

| | skill | workflow (`wf`) |
|---|---|---|
| What | agent-owned context + recorded macro | executable automation module |
| Inputs | none — replays fixed steps | typed, validated spec |
| Composes | no — stops at first failure | yes — `ctx.call()`, cycle + budget fuses |
| Trust | untrusted guidance (may go `stale`) | operator-trusted code (same as shell) |

```bash
# save, run, and iterate — no filesystem access needed
cloakctl wf save title --code 'META = {"inputs": {"url": {"type": "str", "required": True}}}
def run(ctx, inputs):
    ctx.navigate(inputs["url"])
    return {"title": ctx.exec("document.title")}'
cloakctl wf run title --profile p --set url=https://example.com --timeout 60
cloakctl wf runs title                     # history + spilled evidence
```

Run against an **open** profile (`cloakctl open p` first — the single-writer lifecycle applies to workflows too).

A reusable site trick → `skill save`. The doing itself compounds (`scrape_list` → `paginate_collect`) → `wf save --code`. Inputs declared `"secret": true` are redacted from every call tree and log.

## Commands

```
cloakctl profiles list|create <name>|rm <name> [--force]
cloakctl open <profile> [--headed] [--engine obscura|cloakbrowser] [--stealth] [--browser-arg <arg>]... [--endpoint <ws>] | close <profile>
cloakctl status [<profile>] | attach <profile> | doctor
cloakctl import <profile> brave [--domain d]... [--dry-run] [-y] | validate <profile> [--url <probe>]
cloakctl navigate <profile> --url <u> | --back | snapshot | diff | wait --text <s> | grep <pattern>
cloakctl act <profile> click|type|fill|key|scroll|select|check|drag --ref e3 [--field e1=a]
cloakctl read [--format markdown|text|links|console] | run "await fetch(...)" | exec "<js>"
cloakctl screenshot [--full] | pdf | download --ref e3 --out-dir <dir> | upload --ref e4 --file <f>
cloakctl tabs list|new|close|active | windows | groups | group <label> <id>...
cloakctl history <profile> | audit <profile> | session <profile> <label>
cloakctl skill save|list|show|search|promote|run|rm <name> ...
cloakctl wf save|run|runs|show|export|rename|prune|rm <name> ...
```

Page text arrives wrapped in `[UNTRUSTED_PAGE_CONTENT nonce=...]` markers — data, not instructions. Cookie values are never printed.

## Remote mode (VPS-side CLI, browser elsewhere)

```bash
cloakctl open linkedin --endpoint wss://cdp.example.com/s/...   # or CLOAKCTL_CDP_URL
cloakctl validate linkedin && cloakctl wf run pc --profile linkedin --new-tab
cloakctl close linkedin        # detaches; the browser keeps running on its host
```

The VPS holds only refs/meta (idle Python, ~0 persistent RAM). Do the cookie `import` where the browser lives; put Cloudflare Access on the tunnel — raw CDP is full browser control.

> Remote mode is **cloakbrowser-only**: obscura's CDP is per-connection
> isolated (a second connection sees an empty session), so the browser host
> runs `open <p> --engine cloakbrowser` before tunneling. An empty endpoint
> is refused outright — never a silent local launch — and `attach` on an
> obscura profile says exactly that.

## Resource profile

Measured 2026-09-18 (`tests/live_load.py`, `tests/live_obscura.py`), whole process tree:

| probe | latency | memory |
|---|---|---|
| obscura cold launch / exec round-trip | ~0.25 s / ~2 ms | ~65 MB RSS |
| Chromium cold launch / snapshot round-trip | ~0.4 s / ~130 ms | ~1.1–1.5 GB RSS |
| 200 navigate+read cycles (obscura) | ~289 ms/cycle, no leak | 65 → 67 MB |
| 4 live profiles (Chromium, worst case) | — | ~5.0 GB RSS |

Budget with the default engine at **~100 MB per profile**; budget Chromium at **~1.2 GB per profile** (2 GB → 1, 4 GB → 2–3, 8 GB → 6). `CLOAKCTL_MIN_MEM_MB` refuses launches below 400 MB free.

## Engine honesty (obscura)

- **One page per profile** — `tabs new` navigates it (reported `navigated: true`); use profiles for isolation.
- **`download` / `history` fail fast** — fetch bytes with `run`-style JS; use `audit` trails. Chromium engine has both.
- **Uploads** need `open <p> --browser-arg=--allow-file-access`; non-file inputs are rejected on both engines.
- **Private/internal IPs are blocked** (SSRF guard) unless opened with `--browser-arg=--allow-private-network`.

> [!NOTE]
> Unattended runs: give every parallel `wf run` on one profile `--new-tab` (owned tabs, closed after) — shared-tab parallelism clobbers snapshot refs.

## Guarantees

- **Single writer** — pid + start-time locks; stale locks reclaimed automatically, zombies never read as live.
- **Crash-safe teardown** — the engine is kernel-tied to its keeper (`PR_SET_PDEATHSIG`); SIGKILL leaves no orphans, a wedged page can't hang `close` (watchdog + per-call deadlines).
- **JSON-always** — failures are JSON documents with nonzero exit; `doctor` explains what to fix.

## Environment

| Env | Purpose | Default |
|---|---|---|
| `CLOAKCTL_ENGINE` | engine for `open` (obscura, cloakbrowser) | obscura |
| `CLOAKCTL_BROWSER` | Chromium binary override (cloakbrowser engine) | auto-detect |
| `CLOAKCTL_HOME` | state dir (per-suite test isolation) | `~/.cloakctl` |
| `CLOAKCTL_PROBE_URL` | `validate` target | LinkedIn feed |
| `CLOAKCTL_MIN_MEM_MB` | launch RAM floor | 400 |

State lives in `~/.cloakctl/profiles/<name>/` + `runtime/` — never edit it directly; every mutation is a CLI verb.

## Contributing & tests

```bash
python -m pytest tests/test_core.py tests/test_wf_prod.py tests/test_engines.py   # 75 unit, no browser
python tests/live_obscura.py        # 29-check obscura matrix (local fixtures, no external network)
python tests/live_matrix8.py        # remote-endpoint matrix
```

PRs: green suite, JSON failures, isolated temp `CLOAKCTL_HOME` per run. Doctrine deep-dives: [docs/SKILLS-VS-WF.md](./docs/SKILLS-VS-WF.md) · plans: [docs/PLAN.md](./docs/PLAN.md), [docs/WORKFLOWS-PLAN.md](./docs/WORKFLOWS-PLAN.md). License: [MIT](./LICENSE).

<!-- tests: 78 unit + 29 obscura live + 14 remote live (matrix8) + 11 smoke -->
