"""cloakctl browser plugin — vendored with cloakctl (github.com/ishan-parihar/cloakctl).

Wraps the `cloakctl` CLI as a BrowserProvider so hermes-agent can select it
via `browser.cloud_provider: cloakctl` in config.yaml.

Engine contract (cloakctl 0.3.1+): the default engine is **obscura** — the
profile's keeper hosts a loopback CDP bridge over its master connection, so
the returned endpoint serves the profile's TRUE session (same page, same
cookies). No Chromium dependency. cloakbrowser stays opt-in via
`browser.cloakctl_engine: cloakbrowser`.

The provider uses a single persistent profile (default: `hermes`) so the
session survives across agent turns — unlike ephemeral cloud sessions, close
is a no-op unless the operator explicitly runs `cloakctl close hermes`.
"""

from __future__ import annotations

from plugins.browser.cloakctl.provider import CloakctlBrowserProvider


def register(ctx) -> None:
    """Register the cloakctl provider with the plugin context."""
    ctx.register_browser_provider(CloakctlBrowserProvider())
