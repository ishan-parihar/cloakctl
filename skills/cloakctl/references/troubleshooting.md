# Troubleshooting — JSON error → cause → fix

Every cloakctl failure is a JSON error document with a nonzero exit code.
Find your error, read the cause, apply the fix.

## Browser lifecycle

| error | cause | fix |
|---|---|---|
| `profile X is not live; run cloakctl open X` | no browser running | `cloakctl open X` |
| `profile X is already live (pid N, cdp port P); use attach` | a live browser owns the profile | keep using it (verbs re-attach) or `cloakctl close X` |
| `no browser binary found` | no Chromium on PATH (cloakbrowser engine) | install one, or `CLOAKCTL_BROWSER=/path/to/chrome`, or use the default: `open <p> --engine obscura` |
| `engine 'obscura' requested but no obscura binary found` | obscura not installed | `./install.sh --obscura-only` (downloads the binary), or `open --engine cloakbrowser` |
| `--endpoint was empty` / `CLOAKCTL_CDP_URL was empty` | remote mode asked for with no URL | set a `wss://...` URL, or unset the env var to launch locally |
| `attach` refuses: `per-connection CDP` | profile runs obscura — no shareable endpoint | remote mode needs Chromium: on the browser host `open <p> --engine cloakbrowser`, then tunnel its ws endpoint |
| warning: `remote browser looks like obscura` | endpoint fronts obscura; verbs would see an EMPTY session | host must relaunch with `--engine cloakbrowser` before tunneling |
| `download: not supported by the obscura engine` | obscura accepts the CDP call but writes no file | fetch bytes with `run`-style JS (`await fetch(url).then(r=>r.blob())`) or use `--engine cloakbrowser` |
| `no History db for profile X` (obscura) | obscura writes no Chromium History sqlite | use `audit <profile>` trails |
| `upload ... disabled` (obscura) | obscura gates `DOM.setFileInputFiles` | re-open: `open <p> --browser-arg=--allow-file-access` |
| `net::ERR_ADDRESS_PRIVATE`-style failures on localhost (obscura) | engine SSRF guard | re-open: `open <p> --browser-arg=--allow-private-network` |
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
| `active` tab jumps between runs | a closed tab left a dangling alias (older builds) | fixed: verbs record what they bind and closing owned tabs clears the alias; for determinism pass `--tab <id>` |
| registry corrupted / lost runs | (should not happen) | atomic writes + interprocess locks; if you see it, report it |

## Diagnostics

```bash
cloakctl doctor          # binary, stale locks, disk, memory, per-profile jar age
cloakctl audit <profile> # command trail (names only — never argv)
cloakctl wf runs <name>  # per-run ok/error/steps/durationMs + spill pointers
scripts/smoke.sh         # end-to-end check on an isolated state dir
```
