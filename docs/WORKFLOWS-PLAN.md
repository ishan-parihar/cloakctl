# cloakctl Workflow Modules — Execution Plan

**Status:** approved for build · **Scope:** browser-only automation infrastructure for AI agents
**Foundation:** production-grade core, 304/304 checks green (unit 58 · matrix1 50 · matrix2 54 · adversarial matrix3 31 · contrast matrix4 13 · wf-infra matrix5 35 · wf red-team matrix6 25 · scale/lifecycle matrix7 22 · automation stress 16) + agentic e2e (known A3 byte-threshold flake aside).

## 1. Intent (locked)

cloakctl is the **browser-agentic powerhouse**: the infrastructure agents use to *create*
reusable automation modules, not just to drive pages. Two layers, strict division:

- **cloakctl** owns everything that touches a live page: verbs, sessions, cookies, tabs,
  extraction, and a growing, versioned arsenal of browser workflows. Nothing else.
- **automaton** is the DAG composition substrate that **includes** cloakctl steps
  (Shell steps over `cloakctl --json`, proven live). It contributes planning, `${}`
  chaining, retries, cron, telemetry, CallFlow joining, non-browser modules — never
  page actuation.

## 2. Language decision (locked): Python-first hybrid

- **Python = control plane.** Workflow definition, input schemas, session/tab management,
  retries, composition, logging, exit codes. In-process with the session; types inputs;
  imports compose natively.
- **JS = data plane.** Page-context execution only (DOM extraction, form introspection,
  scroll-collect) via the existing `exec`/`run` bridge, shipped as a versioned
  `pagefns/` library. No JS-authored workflows — one module language, no bifurcated
  registry, no cross-language composition boundary.

## 3. Architecture

```
Agents ── CLI ───────────────────────────────────────────────────
              verbs (atomic)    skill run (replay)    wf run (modules)
                 │                  │                     │
                 ▼                  ▼                     ▼
          pageops/tabs/snap ◄── sdk.Context ──► wf registry (~/.cloakctl/workflows/)
          cdp/cookies/hist          │  delegates, no CDP duplication
                                    ▼
                              pagefns/ (versioned in-page JS)
Automaton ── Shell ──► `cloakctl wf run <name> --input {...}` ── JSON out
Hermes provider ──► same CLI surface
```

Three tiers, one transport (`--json` CLI), no servers, no new processes:

| Tier | Form | Use |
|------|------|-----|
| 1 · Verbs | `navigate/snapshot/act/read/…` | atomic page ops (exists) |
| 2 · Skills | JSON macros | exact replay + `skill promote` → Tier-3 scaffold (exists) |
| 3 · Workflows | Python `def run(ctx, inputs) -> dict` + manifest | typed, parameterized, composable modules (new) |

`sdk.Context(profile)` wraps today's internals and returns plain dicts, plus semantic
addressing (`click(role,name)`, `fill_form({label: value})`, `extract_table(sel)`) so
workflows don't depend on brittle `eN` refs. `ctx.call(name, inputs)` runs registry
workflows as logged sub-runs; `ctx.paginate(...)` covers bounded iteration so recursion
is never needed structurally.

### Loop protection (compounding without compute-eating cycles)

1. **Static, on `wf save`:** dependency graph from manifest `depends_on` + import scan;
   `toposort` failure → save refused with the cycle path. A loop can never be stored.
2. **Dynamic, at run:** call-stack of workflow names (ancestor re-entry → abort `cycle`),
   max depth (default 8), step budget per run (default 500 primitive calls → abort).
   Any fuse fires → JSON error, never a hang.
3. Declared bounded recursion only: self-reference allowed iff manifest sets
   `recursive: true` with a `max_pages`/bound. Run log records the full call tree
   (parent → children + durations) so compounding is visible.

## 4. Stages

### S1 — `cloakctl.sdk` facade
`Context(profile)`: navigate / snapshot(+diff) / act / read / grep / exec / run /
tabs / history / cookies-validate passthroughs returning dicts; semantic helpers
(find-by-role+name, fill_form, extract_table/links); `ctx.js()` pagefn runner;
`ctx.call()` sub-workflow runner with depth/budget accounting; `ctx.paginate()`.
**Accept:** every helper live-covered; zero duplicated CDP code (delegation only).

### S2 — `wf` registry
`wf save/list/show/run/search/export/log`; file-backed
`~/.cloakctl/workflows/<name>/{module.py,manifest.json,runs.jsonl}`; manifest
`{name, version, description, site, inputs-schema, depends_on[], recursive, max_depth,
max_steps}`; input validation with JSON errors; cycle detection on save; run log with
call tree + durationMs; exit codes mirror `skill run`.
**Accept (matrix5):** CRUD green · bad-input rejected · cyclic save refused with path ·
ancestor re-entry aborts `cycle` · step-budget abort fires · 3-deep composed workflow
(login → scrape → collect) green with correct call tree.

### S3 — Seed arsenal (all parameterized, all browser-only)
`form_fill` · `scrape_table` · `scrape_list` · `paginate_collect` · `login_check` ·
`download_collect`.
**Accept:** each re-runs green on a fresh profile against fixtures (extends live_auto).

### S4 — `skill promote`
Macro → Python scaffold with `TODO(semantic)` markers where refs were used.
**Accept:** promoted order-form runs after ref replacement.

### S5 — DROPPED (self-contained direction)
No bridge code lives in cloakctl. External orchestrators consume the
`--json` CLI; `docs/AUTOMATON-BRIDGE.md` was removed. The composition
surface they use is `wf run` (exit codes + run documents), proven in
`tests/live_matrix5.py`.

### S5 (original, superseded) — Automaton inclusion bridge (thin)
`cloakctl-shell` module pattern (one Shell step = one `wf run`, exit-code → status);
reference flows: scrape DAG, cron login-check, CallFlow join into a non-browser module;
E2E re-run against `wf run`. Contract paragraph in both READMEs: *browser steps are
cloakctl invocations; automaton never re-implements page actuation.*
**Accept:** all three flows green.

### S6 — Evolution loop
`wf show` age/clean-run ratio; `doctor` nudge on repeated un-saved audit sequences
(suggest-only); deprecate-by-staleness.
**Accept:** nudge fires on synthetic repeat traffic; stale skill flagged.

### S7 — Production-grade, CLI-native compounding (done 2026-09-16)
Agents manage the whole lifecycle without filesystem access: `wf save --code |
--file -` (stdin), `wf show --source`, `wf rm` (builtins refuse), `promote`
returns the scaffold inline with a `next` resave recipe. Modules are
self-describing: `META` supplies version/description/site/inputs/depends_on
when flags are omitted. Runs are bounded three ways — depth + step budgets
plus a process wall-clock (`wf run --timeout`, POSIX `setitimer`; cooperative
`ctx.checkpoint()` for compute loops) — and parallel-safe via `wf run
--new-tab` (owned tab, closed best-effort afterwards). Inputs declared
`"secret": true` are redacted from every call tree, run document, and log;
the module still sees the real value. Every run document carries a `call`
root echo (redacted inputs + version) above the `callTree`.
**Trust boundary (locked):** wf modules are operator-trusted code, same as
shell. Page content is data-only (proven: hostile JSON-shaped page text
round-trips as data, never code). `record_active` is last-writer-wins on an
advisory hint only — always validated against live targets, safe under
concurrent runs. Skills-vs-workflows doctrine lives in
docs/SKILLS-VS-WF.md: skills carry the how-to-think (agent context + static
replay, no inputs, no composition); workflows carry the how-to-do (typed,
composable, budgeted). Promotion is one-way, explicit, DRAFT-marked.
**Accept:** tests/live_matrix6.py 25/25 adversarial green
(spin/checkpoint kill, output-bomb spill, sub-call redaction, static-evasion
cycle fuse, parallel tab isolation, injection-as-data) + tests/test_wf_prod.py
unit green + full regression (matrices 1–5, auto, agentic) green.

### S8 — Scale, conflicts, native lifecycle (done 2026-09-16)
Red-team drove the design: 20 parallel `wf log-run` lost 6 entries
(read-modify-write race) → registry mutations now go through atomic
writes + interprocess locks; histories capped (200 runs, 50 imports,
rotating audit). New native verbs close the lifecycle loop:
`wf rename/runs/prune`, `wf runs --output <stamp>` (spilled evidence
without FS), `skill rm/rename`, `skill run --timeout`, `wf export
--json`. Name hygiene locked: strict `[A-Za-z0-9-_]` registry names at
save (no sanitizer collisions), profile names validated on every path
builder (traversal refused as JSON). `groups list` self-heals dead tab
labels; `close_window` cleans its groups; `wait` polls tolerate single
slow CDP round-trips; browser-binary home search cached.
**Accept:** tests/live_matrix7.py 22/22 (parallel-save convergence,
20/20 log storm, 3-way --new-tab isolation, replay timeout, restart
healing) + unit 58/58 + full regression green.

### S9 — Remote-endpoint mode (done 2026-09-16)
VPS-side CLI, browser on another host: `open --endpoint wss://…`
(`CLOAKCTL_CDP_URL` fallback) attaches without launching — no Popen, no
signals, no DevToolsActivePort; reachability (WS handshake + round-trip)
is liveness. `close` detaches. Status reports `remote: true` (version via
`Browser.getVersion`, jar fingerprint over WS); doctor reports `rssMB:
null` for remote (never a pid-0 machine sum). File honesty: screenshot/pdf
bytes travel over the socket; download confirms via downloadProgress events
and lands browser-side (`dir` + `guid`); upload paths must exist
browser-side (errors say so). Import where the browser lives; Access on
the tunnel. VPS footprint: idle Python, ~0 persistent RAM.
**Accept:** tests/live_matrix8.py 12/12 two-HOME red-team green.

## 5. Non-goals (explicit)

No cron/scheduler in cloakctl · no DAG/branching primitives in the registry (it's
Python — `for`/`if` exist) · no Rust · no server · no non-browser SDK surface ·
no `run`-JS-SDK parity build (composition lives in automaton) · no screenshot-annotate
or JS-dialog verbs (no CDP-side primitive; revisit if Chromium adds APIs).

## 6. Test strategy

Extend the live-matrix series, same rules as the foundation: isolated `CLOAKCTL_HOME`
per suite, fixture HTTP server where needed, zero stray processes asserted at teardown,
no tracebacks, JSON-always. New: `tests/live_matrix5.py` (S2 acceptance) + arsenal
cases folded into `tests/live_auto.py`.

Hard lesson, kept: every subprocess harness MUST pass `env=ENV` with the
suite's temp `CLOAKCTL_HOME` — a missing env silently splits the registry
(save lands in `~/.cloakctl`, run looks in temp) and the failure
masquerades as product behavior. `tests/live_matrix6.py` caught exactly
this during its own development; the pollution was removed.
