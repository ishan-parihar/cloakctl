# Remote VPS topology — browser here, CLI there

cloakctl's CLI can run where the browser isn't: `open --endpoint` attaches
over a tunnel instead of launching. The VPS side holds only refs/meta and is
idle Python (~0 persistent RAM); the ~1.1 GB browser lives on the host that
has it.

## Topology

```
VPS (2.5GB, heavy infra)                browser host (this machine)
  cloakctl CLI ── wss:// ── cloudflared ── Chrome/149 + profiles
  ~/.cloakctl/{workflows,skills,     ~/.cloakctl/profiles/<name>/chrome/
               runtime/refs,meta}    (the session itself)
```

## Browser host setup

```bash
cloakctl open linkedin                      # launch normally, note wsEndpoint
cloudflared tunnel ...                      # publish the CDP route
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
- Browser host: ~1.2 GB RAM per live profile (2 GB → 1 profile, 4 GB →
  2–3, 8 GB → 6). `CLOAKCTL_MIN_MEM_MB` refuses launches below 400 MB free.
- Latency: snapshot is ~4 CDP round-trips on a ~130 ms local base; add
  tunnel RTT per round-trip (typically +20–50 ms on Cloudflare).
