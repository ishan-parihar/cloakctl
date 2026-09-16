# Troubleshooting — JSON error → cause → fix

Every cloakctl failure is a JSON error document with a nonzero exit code.
Find your error, read the cause, apply the fix.

## Browser lifecycle

| error | cause | fix |
|---|---|---|
| `profile X is not live; run cloakctl open X` | no browser running | `cloakctl open X` |
| `profile X is already live (pid N, cdp port P); use attach` | a live browser owns the profile | keep using it (verbs re-attach) or `cloakctl close X` |
| `no browser binary found` | no Chromium on PATH | install one, or `CLOAKCTL_BROWSER=/path/to/chrome` |
| `CLOAKCTL_BROWSER='...' not found on PATH` | override points nowhere | fix the path |
| `remote endpoint unreachable` | tunnel down / wrong URL | check the tunnel; retry `open --endpoint` |
| stale lock reported by doctor | browser was SIGKILLed | `open` reclaims automatically; `doctor` was just telling you |

## Page actuation

| error | cause | fix |
|---|---|---|
| `no snapshot refs for this tab; run snapshot first` | refs never taken | `snapshot <profile>` |
| `ref eN not in last snapshot; re-run snapshot` | page navigated since | re-`snapshot` (refs persist per tab) |
| `act: element has no visible box` | off-viewport / display:none | scroll first, or fall back to `act click` (auto `el.click()`), or `--x/--y` |
| `wait: timeout after Ns` | text/selector never appeared | check spelling; use `--selector`; raise `--timeout` |
| `download: timeout` | slow host / remote file | remote: it lands browser-side, see `dir`+`guid` in the doc |
| `upload: not a file: X` | path missing | exists on this machine? remote profiles: stage on the BROWSER host |

## Workflows

| error | cause | fix |
|---|---|---|
| `wf save: exactly one of --code/--file` | both given | pick one |
| `workflow name 'x': use 1-64 chars of [A-Za-z0-9-_]` | bad name (slash, leading dash, space) | rename to a strict name |
| `depends_on 'x': unknown workflow` | dep not saved | `wf save` the dep first |
| `cyclic dependency` (WfCycleError) | a→b→a graph | restructure; the registry refuses cycles |
| `WfTimeoutError` in run doc | wall-clock exceeded | raise `--timeout`; the module can `ctx.checkpoint()` to yield |
| `inputs[k]: need {type: ...}` | bad input spec | use one of str/int/float/bool/json |
| `no workflow 'x'` (after rename) | old name retired | use the new name — rename is explicit, never silent |
| `cannot remove builtin workflow` | rm on a seeded module | builtins are protected; copy+edit under a new name |
| `run <stamp>: no matching stamp` | output prefix wrong | `wf runs <name>` lists stamps; copy one |

## Concurrency + scale

| symptom | cause | fix |
|---|---|---|
| runs clobber each other's refs | parallel `wf run` on one shared tab | pass `--new-tab` (owned tab per run) |
| `active` tab jumps between runs | last-writer-wins on the alias | it's advisory + validated; pass `--tab <id>` for determinism |
| registry corrupted / lost runs | (should not happen) | atomic writes + interprocess locks; if you see it, report it |

## Diagnostics

```bash
cloakctl doctor          # binary, stale locks, disk, memory, per-profile jar age
cloakctl audit <profile> # command trail (names only — never argv)
cloakctl wf runs <name>  # per-run ok/error/steps/durationMs + spill pointers
scripts/smoke.sh         # end-to-end check on an isolated state dir
```
