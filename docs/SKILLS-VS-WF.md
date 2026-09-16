# Skills vs workflows (the one distinction)

They are different tiers with different authors and different trust:

| | skill | workflow (`wf`) |
|---|---|---|
| **What** | agent-owned context + a recorded macro | executable automation module |
| **Author** | the agent, in its own words | the agent, as Python |
| **Form** | markdown notes + static argv replay | `run(ctx, inputs)` + `META` |
| **Inputs** | none — a skill replays fixed steps | typed, validated (`inputs` spec) |
| **Composes** | no — `skill run` stops at the first failure | yes — `ctx.call()` with cycle + budget fuses |
| **Manages** | `skill save/list/show/run/rm/rename/search/promote` | `wf save/list/show/search/run/runs/rename/prune/export/rm` |
| **Trust** | untrusted guidance (may be stale — see `stale`) | operator-trusted code (same as shell) |

The rule: **skills carry the *how-to-think*, workflows carry the
*how-to-do*.** An agent writes a skill when it learns context worth reusing
("linkedin login needs X before Y", "this site paginates with a More
button"). An agent writes a workflow when the doing itself is reusable,
parameterized, and worth compounding (`scrape_list` → `paginate_collect`
→ site-specific collectors via `ctx.call`).

Promotion is one-way and explicit: `skill promote` scaffolds a DRAFT
workflow with `TODO(semantic)` markers; the agent hardens refs, then saves
it back with `wf save --code` — no filesystem round-trip. Workflows never
downgrade into skills; if a workflow rots, its run stats (`lastOk`,
`stale`) say so and the agent rewrites the module, not a note about it.

Secrets: a workflow input declared `"secret": true` is redacted from every
call tree, run document, and log. Skill notes are plain text — never paste
credentials into a skill; pass them as secret workflow inputs instead.

Lifecycle recipes (all CLI-native, no filesystem):

- Iterate a module: `wf show <n> --source` → edit → `wf save <n> --code
  '...'` (run history survives re-saves).
- Debug a failure: `wf runs <n>` (errors, durations, sub-call depth) →
  `wf runs <n> --output <stamp>` (spilled large outputs, trust-wrapped).
- Curate at scale: `wf rename` (history moves with it), `wf prune --keep
  20` (trims history + orphaned spill dirs), `wf rm` (builtins refuse).
- Skills: `skill show` carries run history + staleness; `skill run
  --timeout` bounds replays; `skill rename` preserves history.

Concurrency contract: one profile, one writer. Parallel `wf run` on the
same profile must pass `--new-tab` (owned tabs, closed afterwards);
the registry itself is race-safe (atomic writes + interprocess locks,
capped histories), proven by `tests/live_matrix7.py`.
