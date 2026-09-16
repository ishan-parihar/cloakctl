# cloakctl — lean cloakbrowser CLI for AI agents (plan)

Status: proposal · 2026-09-10
Owner: ishan · Sibling of `internet/browsefleet`

## 1. Goal

One persistent, stealth browser per profile on this machine, reachable by any AI
agent through a clean CLI, with **first-class import of the operator's active
browser session cookies** (Brave today, Chrome/Firefox tomorrow).

Non-goals (deliberately): no HTTP REST API, no pool, no session registry, no
billing/metering, no SSE, no agent layer, no captcha, no egress probing. One
machine, one CLI, N profiles, zero network surface beyond the CDP socket.

## 2. Why a CLI, not BF, for this workload

Proven over the linkedin-lyr audit (2026-09-08 → 09-10):

- **Sessions on one profile must be serialized.** Parallel sessions on the same
  jar are a LinkedIn risk signal; BF's pool invites parallelism.
- **The create-time cookie payload is the burn vector.** #2601b/#2601c: any
  path that carries cookies across contexts can replay a revoked jar. The only
  safe jar lives inside its own browser. A persistent context makes injection
  structurally impossible — there is no session boundary to smuggle across.
- **Agent ergonomics want verbs, not REST.** `cloakctl open linkedin` beats
  `POST /v1/sessions` + `connect_over_cdp` + `release` for a single-host agent.

## 3. Command surface (v1)

```
cloakctl profiles list|create <name>|rm <name>
    Profile = one cloakbrowser persistent user-data-dir under
    ~/.cloakctl/profiles/<name>/. Metadata in meta.json (UA mint, created,
    last-used, notes). No central database.

cloakctl open <profile> [--headless|--headed] [--cdp-port <port>]
    Launch (or re-attach to) the persistent context for <profile>.
    Prints: PID, CDP port, ws endpoint. Idempotent: if the profile already
    has a live browser, print its endpoint and exit 0 (never a second chrome).

cloakctl close <profile>
    Graceful shutdown of that profile's browser (flushes cookies to disk).

cloakctl status [profile]
    Live? PID, uptime, CDP endpoint, last navigation, cookie-jar fingerprint
    (first 8 hex of sha256 over sorted (name,value) — read-only, no navigation).

cloakctl import <profile> [--from brave|chrome|firefox|file:<path>] [--dry-run]
    THE core feature. Imports the operator's active browser cookies into the
    profile. Sub-behaviors:
      - Copies cookies into the profile's cookie store via CDP `Storage.setCookies`
        against the *already-running* persistent browser (same-context write,
        not a cross-context replay).
      - --dry-run: print what would be imported (names, domains, expiry),
        plus a risk note when a cookie is <24h old (fresh logins are the
        likeliest to be challenge-susceptible on first foreign use).
      - After import, forces one in-browser validation navigation
        (linkedin.com → check authwall) and prints the verdict. Never emits
        cookies to stdout, never writes them outside the profile dir.
    This is explicitly OPT-IN per invocation — the linkedin-lyr default
    remains "profile-native session only".

cloakctl exec <profile> -- <js>
    Evaluate JS in the profile's active page. For agents: the only
    programmatic surface needed alongside CDP.

cloakctl attach <profile>
    Print the CDP ws endpoint (for Playwright/Puppeteer connect_over_cdp).

cloakctl doctor
    Binary present+version, port conflicts, stale locks, disk usage,
    per-profile cookie fingerprint + age.
```

Design rules baked into v1:

- **Single-writer enforcement.** `open` on a live profile re-attaches; it never
  launches twice. This is BF-fix-2's guarantee, enforced by a per-profile
  lockfile (PID + start time) checked before launch, plus the Chromium
  SingletonLock as backstop.
- **No cookie egress.** Cookies never leave the machine via the CLI; the only
  copy operations are (a) OS keyring/DB → profile dir at import time,
  (b) profile dir → Chrome at launch (native persistence).
- **Everything observable.** Every command prints a machine-readable `--json`
  variant so agents can parse without scraping text.

## 4. Architecture

```
~/.cloakctl/
  profiles/<name>/
    chrome/          ← cloakbrowser persistent user-data-dir (the session)
    meta.json        ← { created, lastUsed, mintedUA, importHistory[] }
  lock.<name>.json   ← { pid, startedAt, cdpPort } while live
  config.toml        ← defaults: headless, cdp port range, browser paths
```

- **Runtime:** Python 3.12 + playwright-core over `connect_over_cdp` for
  control, but launch via cloakbrowser's own CLI/binary directly
  (`cloakbrowser launch --user-data-dir ... --remote-debugging-port=0`).
  Node stays inside BF; cloakctl is self-contained Python so linkedin-lyr's
  existing asyncio code can call it as a library OR subprocess.
- **Discovery of the CDP port:** `--remote-debugging-port=0` writes
  `DevToolsActivePort` in the profile dir — cloakctl reads it. No port guessing.
- **Supervision:** `open` starts the browser detached (setsid) with a pidfile;
  a tiny `cloakctl supervisor` (optional systemd user unit) reconciles desired
  vs. actual state — restart on crash, weekly memory-recycle window.

## 5. Import pipeline (the hard part)

1. **Extract** — per browser:
   - Brave/Chrome: read `Cookies` SQLite (copy DB + keyring-decrypt
     `v10/v20` values; reuse the proven read-only extractor pattern from the
     linkedin-lyr audit scripts).
   - Firefox: `cookies.sqlite` (no encryption).
   - `file:`: a cookies.json the user exported deliberately.
2. **Transform** — filter to user-specified domains (default: none → all),
   drop expired, map to CDP cookie dicts, preserve `expires/httpOnly/secure/
   sameSite/priority`.
3. **Validate-in-context** — write via CDP into the running persistent
   browser, then navigate a probe URL in-browser and classify:
   `logged_in | anonymous | challenged | burn_signature`.
   A `burn_signature` (li_at=delete-me / clear-site-data) on first foreign use
   is reported loudly; the import is recorded in meta.json history either way.
4. **Record** — meta.json gains `{ts, source, domains, verdict, jarFingerprint}`.

The key architectural property: **import writes into the same browser context
that will use the session.** There is no second context to burn. (The
2026-09-08 in-browser replay experiment showed same-context writes are how the
browser itself refreshes cookies — safe by construction.)

## 6. Security posture

- CDP socket binds `127.0.0.1` only. Remote agents reach it through the
  existing cloudflared tunnel **with Cloudflare Access on the WSS route**
  (raw CDP = full browser control incl. cookies — must never be anonymous).
- Profile dirs `chmod 700`; meta.json/cookie artifacts never contain values,
  only fingerprints.
- `import` requires the browser to be running (same-context guarantee) and
  prints a diff-style confirmation unless `--yes`.

## 7. Build plan

| Phase | Deliverable | Est. |
|---|---|---|
| P1 | `profiles`, `open`, `close`, `status`, `attach`, `doctor` + lockfile single-writer | 1 day |
| P2 | `import` for Brave (keyring decrypt) + dry-run + in-context validation | 1 day |
| P3 | `exec`, `--json` everywhere, supervisor unit, Firefox/Chrome extractors | 1 day |
| P4 | linkedin-lyr `LINKEDIN_BROWSER_BACKEND=cloakctl` (200-LOC client) and parallel-run vs BF for a week | 0.5 day |

Acceptance for P2 (the linkedin-lyr bar):
- Import with a live Brave jar → in-context validation reports `logged_in`,
  zero network calls outside the probe navigation, Brave's own jar untouched.
- Double-`open` returns the same CDP endpoint (no second chrome).
- Kill -9 the browser → `doctor` reports the stale lock; `open` recovers.

## 8. Relationship to BrowseFleet

- BF remains the multi-consumer/multi-session product (scrape, screenshot,
  captcha, agent). The three 2026-09-10 fixes (expire-leak, profile 409,
  gated save-on-release) make BF safe for profile-bound workloads too.
- cloakctl does not replace BF for fleet use-cases; it replaces BF *for the
  one-account stealth-session workload* where its pool/REST layers are
  overhead and its create-time cookie payload is a liability.
- If cloakctl proves out (P4), a future `bf serve --single` could reuse the
  same single-persistent-context shape inside BF's codebase.
