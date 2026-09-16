# Workflows and skills — the registry in depth

The registry lives under `~/.cloakctl/`: `workflows/<name>/` (module.py +
manifest.json + runs/<stamp>/ spill) and `skills/<name>.json`. Everything is
inline CLI — never edit these files directly.

## When to write what

**Skill** = agent-owned context + recorded macro. You write one when you
learn reusable context: pagination patterns, site quirks, login flows. It
has NO inputs and replays fixed argv steps, stopping at the first failure.

```bash
cloakctl skill save paginate-tips --description "Site X paginates with a More button; 2s settle" \
  --steps '[{"cmd": ["act", "x", "click", "--ref", "e3"]}, {"cmd": ["wait", "x", "--timeout", "3"]}]'
cloakctl skill run paginate-tips          # rc=1 at first failing step
cloakctl skill log-run paginate-tips --failed --note "e3 stale after nav"
```

**Workflow** = typed, composable automation. You write one when the doing
itself is reusable:

```bash
cloakctl wf save scrape_list --file - <<'EOF'
"""Extract a list of {text, href} items from one page."""
META = {
    "version": "1.0.0",
    "description": "Extract text+href items matching a selector.",
    "inputs": {
        "url": {"type": "str", "required": True},
        "selector": {"type": "str", "required": True},
    },
    "depends_on": [],
}
def run(ctx, inputs):
    ctx.navigate(inputs["url"])
    return {"count": len(ctx.extract_list(inputs["selector"])),
            "items": ctx.extract_list(inputs["selector"])}
EOF
```

**Promotion** is one-way and explicit: `skill promote <name> [--to <wf>]`
scaffolds a DRAFT module with `TODO(semantic)` markers from the skill's
steps. Harden the refs (snapshot first), then `wf save --code` the result.

## The ctx API (inside a wf module)

```
ctx.navigate(url, tab=None)          ctx.snapshot(tab=None) -> {refs, snapshot, diff}
ctx.act(kind, ref=None, ...)         ctx.wait(text=None, selector=None, timeout=15)
ctx.read(fmt="text")                 ctx.extract_list(selector) -> [{text, href}]
ctx.exec(js)                         ctx.call("<wf-name>", inputs_dict) -> outputs
ctx.checkpoint()                     # cooperative yield (compute loops)
ctx.log(note)                        # structured note in the run doc
```

- `ctx.call` composes workflows (3-deep works fine); the parent's run doc
  carries a `callTree` and the child's redacted `call` echo.
- Every verb is budgeted: `max_depth` (default 8), `max_steps` (500), and
  the wall-clock `wf run --timeout` (SIGALRM). A stack fuse catches static
  evasion (`"r_" + "ecursion"`); dynamic ancestor re-entry dies too.

## Inputs + secrets

`inputs` is a spec: `{"type": "str|int|float|bool|json", "required": bool,
"description": ..., "secret": bool}`. Validate `--input '{"url": ...}'` or
flatten with repeated `--set k=v`. Inputs declared `"secret": true` are
redacted from every call tree, run document, and log — the module still sees
the real value at runtime. Declare secrets for anything credential-shaped.

## Run discipline

- Parallel on one profile → `wf run --new-tab` (owned tab, closed
  afterwards). Shared-tab parallel runs clobber snapshot refs.
- `--timeout` bounds wall-clock; the module may call `ctx.checkpoint()` in
  compute loops to yield to it.
- Outputs > 4000 chars spill to `runs/<stamp>/outputs.json`; fetch without
  FS via `wf runs <name> --output <stamp-prefix>` (trust-wrapped).
- Failures are JSON: `{"ok": false, "error": "WfTimeoutError: ...", ...}`
  with rc=1. `wf runs <name>` shows per-run `durationMs`, `steps`, errors.

## Lifecycle

```
wf list | show <name> [--source] | search <text> | export <name> [--out file]
wf save <name> --code '...' | --file mod.py | --file -
wf rename old new            # history moves with it
wf runs <name> [--output <stamp>] | prune <name> [--keep 50] | rm <name>
```

- Re-save keeps run history (version up in META when behavior changes).
- Strict names: 1–64 chars `[A-Za-z0-9-_]`, leading alnum — saves are
  gated so two raw names can never collide on one sanitized slot.
- `wf prune` trims history to `--keep` and drops orphaned spill dirs.
- Builtins (scrape_list, paginate_collect, ...) refuse `rm`/`rename`/`prune`.

## Doctrine (why the split exists)

Skills are untrusted *guidance* — they may go stale, they carry judgment
("wait 2s because the site settles late"). Workflows are operator-trusted
*code* — typed, budgeted, composable, redaction-aware. Mixing them (steps
that need inputs, or modules that only re-record argv) blurs both: a skill
that needs inputs wants to be a wf; a wf that only replays fixed steps and
carries judgment wants to be a skill. Promotion is the one bridge, and it's
deliberately one-way with human review (`TODO(semantic)` markers).
