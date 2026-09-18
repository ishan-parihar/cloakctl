# Remote VPS topology — browser here, CLI there

cloakctl's CLI can run where the browser isn't: `open --endpoint` attaches
over a tunnel instead of launching. The VPS side holds only refs/meta and is
idle Python (~0 persistent RAM); the browser lives on the host that has it.

> **Remote mode is a cloakbrowser (Chromium-family) feature.** obscura's CDP
> is per-connection isolated — a second connection to `obscura serve` gets
> its own empty page and an empty cookie jar — so a tunneled obscura serve
> would accept the attach and then run every verb against an EMPTY session.
> The browser host must launch the profile with `--engine cloakbrowser`
> before you tunnel it.
>
> **Local attach on obscura works via the keeper bridge**: the keeper hosts
> a loopback CDP endpoint over its master connection — the profile's TRUE
> session (same page, same cookies). `cloakctl attach <p>` prints it; hermes
> /agent-browser/playwright attach there. Tunneling it is not recommended:
> loopback + per-keeper token is designed for same-host clients only.

## Topology

```
VPS (2.5GB, heavy infra)                browser host (this machine)
  cloakctl CLI ── wss:// ── cloudflared ── Chromium + profiles
  ~/.cloakctl/{workflows,skills,     ~/.cloakctl/profiles/<name>/chrome/
               runtime/refs,meta}    (the session itself)
```

## Browser host setup

```bash
cloakctl open linkedin --engine cloakbrowser   # launch, note wsEndpoint
cloudflared tunnel ...                         # publish the CDP route
```

The tunnel MUST carry Cloudflare Access (raw CDP is full browser control —
cookies, navigation, everything; it must never be anonymous). Plain
`ws://127.0.0.1` attachments get a loud stderr warning; never expose one.

## VPS setup

```bash
git clone https://github.com/ishan-parihar/cloakctl && cd cloakctl
./install.sh                                # no browser needed on the VPS
export CLOAKCTL_CDP_URL=wss://cdp.example.com/s/...   # or per-call --endpoint
cloakctl open linkedin                      # remote: true, reattach-safe
# (an empty endpoint/env var is REFUSED, never a silent local launch)
cloakctl validate linkedin                  # logged_in | anonymous | challenged | burn_signature
cloakctl snapshot linkedin && cloakctl act linkedin click --ref e3
cloakctl wf run pc --profile linkedin --new-tab --set url=https://... --set selector=a
cloakctl close linkedin                     # detaches, never kills
```

`close` on a remote profile detaches only (`{closed: true, detached: true}`)
— the browser keeps running on its host. Liveness is reachability (a WS
handshake + round-trip); a dead host reads as clean `live: false` JSON.

## What works over the socket

- All verbs: navigate/snapshot/act/wait/read/grep/exec/run, tabs, wf runs
- `import`/`validate`: do these on the browser host (they read the local
  browser DBs and keyring; the CDP write itself is same-context)
- `screenshot`/`pdf`: bytes travel over the socket, saved VPS-side
- `download`: lands browser-side (the file is invisible to the VPS);
  confirmed via `Browser.downloadProgress` events — returns `dir` + `guid`
- `upload`: paths must exist on the BROWSER host — stage the file there
  first (errors say so)

## Sizing

- VPS: idle Python; the whole state dir (refs, manifests, skills) is KBs.
  Any box works — no browser, no Chromium, no X.
- Browser host (required here — remote mode is Chromium-family):
  ~1.2 GB RAM per live profile (2 GB → 1 profile, 4 GB → 2–3, 8 GB → 6).
  `CLOAKCTL_MIN_MEM_MB` refuses launches below 400 MB free.
  (obscura stays the local default at ~100 MB/profile; it just can't be
  tunnel-shared.)
- Latency: snapshot is ~4 CDP round-trips on a ~130 ms local base; add
  tunnel RTT per round-trip (typically +20–50 ms on Cloudflare).
