"""cloakctl browser plugin — vendored with cloakctl (github.com/ishan-parihar/cloakctl).

Wraps the `cloakctl` CLI as a BrowserProvider so hermes-agent can select it
via `browser.cloud_provider: cloakctl` in config.yaml.

Engine contract (cloakctl 0.3.0): the provider opens profiles with
`--engine cloakbrowser` because obscura (the cloakctl default) has
per-connection-isolated CDP and cannot serve external attach. The provider
uses a single persistent profile (default: `hermes`) so the session survives
across agent turns — unlike ephemeral cloud sessions, close is a no-op unless
the operator explicitly runs `cloakctl close hermes`.
"""

from __future__ import annotations

from plugins.browser.cloakctl.provider import CloakctlBrowserProvider


def register(ctx) -> None:
    """Register the cloakctl provider with the plugin context."""
    ctx.register_browser_provider(CloakctlBrowserProvider())
