"""Python control plane for workflow modules (Tier 3).

`Context` is a thin typed facade over the Tier-1 verbs: every method
delegates to `pageops`/`tabs`/`cookies`/`hist` and returns plain dicts —
no CDP code is duplicated here. Semantic helpers (find/fill/extract)
compose verb calls with the versioned `pagefns` JS library.

Budgets (step count, call depth, cycle stack) live on `RunState`, shared
by a run and all its sub-workflow calls.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from . import cookies, hist, pagefns, pageops, snap, tabs as tabs_mod

DEFAULT_MAX_STEPS = 500
DEFAULT_MAX_DEPTH = 8


class WfError(RuntimeError):
    pass


class WfCycleError(WfError):
    pass


class WfBudgetError(WfError):
    pass


class WfTimeoutError(WfError):
    pass


class WfInputError(WfError):
    pass


class RunState:
    """Per-run accounting shared across composed workflow calls."""

    def __init__(self, owner: str, max_steps: int = DEFAULT_MAX_STEPS,
                 max_depth: int = DEFAULT_MAX_DEPTH,
                 deadline: float | None = None):
        self.owner = owner
        self.max_steps = max_steps
        self.max_depth = max_depth
        self.deadline = deadline  # monotonic seconds, or None
        self.steps = 0
        self.stack: list[str] = [owner]
        self.tree: list[dict] = []  # root call nodes

    def bump(self, n: int = 1) -> None:
        if (self.deadline is not None
                and time.monotonic() > self.deadline):
            raise WfTimeoutError(
                f"run {self.owner!r}: wall-clock deadline exceeded")
        self.steps += n
        if self.steps > self.max_steps:
            raise WfBudgetError(
                f"run {self.owner!r}: step budget exceeded "
                f"({self.steps} > {self.max_steps})")


_REF_LINE = re.compile(r"^(\s*)- (\S+)(?: '([^']*)')? \[ref=(e\d+)\]")


class Context:
    """A bound browser session for one workflow run."""

    def __init__(self, profile: str, tab: str | None = None,
                 state: RunState | None = None,
                 node: dict | None = None):
        self.profile = profile
        self.tab = tab
        self.state = state or RunState(profile)
        self._node = node  # call-tree node for THIS workflow (None at top)

    # -- verb delegates (each is one budgeted step) -------------------------

    def checkpoint(self) -> None:
        """Enforce deadline + step budget inside pure-compute loops.

        Verb calls check automatically; a Python-only loop that never
        touches the browser must call this per iteration. The process
        wall-clock (`wf run --timeout`) still applies regardless."""
        self.state.bump(0)

    def _step(self, fn, *args, **kwargs):
        self.state.bump()
        return fn(*args, **kwargs)

    def navigate(self, url: str) -> dict:
        return self._step(pageops.do_navigate, self.profile, "url", url, self.tab)

    def back(self) -> dict:
        return self._step(pageops.do_navigate, self.profile, "back", None, self.tab)

    def forward(self) -> dict:
        return self._step(pageops.do_navigate, self.profile, "forward", None, self.tab)

    def reload(self) -> dict:
        return self._step(pageops.do_navigate, self.profile, "reload", None, self.tab)

    def snapshot(self, depth: int = 12, mode: str = "ax") -> dict:
        return self._step(pageops.do_snapshot, self.profile, self.tab, depth, mode)

    def diff(self) -> dict:
        return self._step(pageops.do_diff, self.profile, self.tab)

    def act(self, kind: str, **kw) -> dict:
        return self._step(pageops.do_act, self.profile, kind, self.tab, **kw)

    def wait(self, text: str | None = None, selector: str | None = None,
             timeout: float = 15.0) -> dict:
        return self._step(pageops.do_wait, self.profile, self.tab, text,
                          selector, timeout)

    def read(self, format: str = "markdown",
             selector: str | None = None) -> dict:
        return self._step(pageops.do_read, self.profile, self.tab, format,
                          selector)

    def grep(self, pattern: str, over: str = "ax", limit: int = 30) -> dict:
        return self._step(pageops.do_grep, self.profile, self.tab, pattern,
                          over, limit)

    def screenshot(self, format: str = "jpeg", quality: int = 80,
                   full: bool = False, out: str | None = None) -> dict:
        return self._step(pageops.do_screenshot, self.profile, self.tab,
                          format, quality, full, out)

    def pdf(self, landscape: bool = False, background: bool = False,
            out: str | None = None) -> dict:
        return self._step(pageops.do_pdf, self.profile, self.tab, landscape,
                          background, out)

    def download(self, ref: str, out_dir: str,
                 timeout: float = 30.0) -> dict:
        return self._step(pageops.do_download, self.profile, self.tab, ref,
                          out_dir, timeout)

    def upload(self, files: list[str], ref: str | None = None,
               selector: str | None = None) -> dict:
        return self._step(pageops.do_upload, self.profile, self.tab, ref,
                          files, selector)

    def exec(self, js: str) -> Any:
        """Evaluate sync JS in the bound tab (raises on JS exceptions)."""
        self.state.bump()
        cdp, _ = pageops._page_client(self.profile, self.tab)
        with cdp:
            return cdp.evaluate(js)

    def run_js(self, code: str, timeout: float = 30.0) -> Any:
        """Evaluate possibly-async JS in the bound tab."""
        self.state.bump()
        cdp, _ = pageops._page_client(self.profile, self.tab)
        old = cdp.timeout
        cdp.timeout = timeout
        try:
            with cdp:
                return cdp.run_program(code)
        finally:
            cdp.timeout = old

    def validate(self, url: str | None = None) -> dict:
        return self._step(cookies.validate_context, self.profile,
                          probe_url=url)

    def history(self, limit: int = 20, query: str | None = None) -> dict:
        return self._step(hist.read_history, self.profile, limit, query)

    def tabs_list(self) -> dict:
        return self._step(tabs_mod.list_tabs, self.profile)

    def tab_new(self, url: str = "about:blank",
                background: bool = False) -> dict:
        return self._step(tabs_mod.new_tab, self.profile, url, background)

    def tab_close(self, target: str) -> dict:
        return self._step(tabs_mod.close_tab, self.profile, target)

    def tab_activate(self, target: str) -> dict:
        return self._step(tabs_mod.activate_tab, self.profile, target)

    # -- pagefn runner (data plane) ------------------------------------------

    def js(self, fn: str, *args, async_: bool = False,
           timeout: float = 30.0) -> Any:
        """Run a versioned pagefn (or raw JS when fn starts with '(')."""
        src = fn if fn.lstrip().startswith("(") else pagefns.get(fn)
        if args:
            call = f"({src})({', '.join(json.dumps(a) for a in args)})"
        else:
            call = src
        if async_ or "async" in src.split("=>")[0].split("(")[0]:
            return self.run_js(call, timeout)
        return self.exec(call)

    def describe_form(self) -> list[dict]:
        return self.js("describe_form") or []

    def describe_page(self) -> dict:
        return self.js("describe_page") or {}

    def extract_table(self, selector: str | None = None) -> dict:
        # Always pass one arg (possibly null): the pagefn is a callable,
        # and an un-invoked source would evaluate to a function object.
        res = self.js("extract_table", selector)
        if isinstance(res, dict) and res.get("ok") is False:
            raise WfError(f"extract_table: {res.get('error')}")
        return res

    def extract_list(self, selector: str) -> list[dict]:
        return self.js("extract_list", selector) or []

    # -- semantic addressing (no brittle eN refs) -----------------------------

    def find_ref(self, role: str | None = None,
                 name: str | None = None) -> str | None:
        """Snapshot the page and return the first ref matching role+name."""
        text = self.snapshot().get("snapshot", "")
        for line in text.splitlines():
            m = _REF_LINE.match(line)
            if not m:
                continue
            _ind, r, nm, ref = m.groups()
            if role and role.lower() not in r.lower():
                continue
            if name and name.lower() not in (nm or "").lower():
                continue
            return ref
        return None

    def ref_for_selector(self, selector: str) -> str | None:
        """Resolve a CSS selector to the current snapshot ref, if any."""
        self.state.bump()
        cdp, tid = pageops._page_client(self.profile, self.tab)
        try:
            doc = cdp.call("DOM.getDocument", {"depth": 0})
            root = doc.get("root", {}).get("nodeId")
            found = cdp.call("DOM.querySelector",
                             {"nodeId": root, "selector": selector})
            node_id = found.get("nodeId")
            if not node_id:
                return None
            desc = cdp.call("DOM.describeNode", {"nodeId": node_id})
            backend = desc.get("node", {}).get("backendNodeId")
        finally:
            cdp.close()
        if backend is None:
            return None
        ref_path, _ = snap._ref_paths(self.profile, tid)
        try:
            refs = json.loads(ref_path.read_text()).get("refs", {})
        except Exception:
            return None
        for ref, b in refs.items():
            if int(b) == int(backend):
                return ref
        return None

    def _match_field(self, fields: list[dict], key: str) -> dict | None:
        kl = key.lower()
        for score in (  # exact label/name/id first, then substring
            lambda f: (f.get("label") or "").lower() == kl
            or (f.get("name") or "").lower() == kl
            or (f.get("id") or "").lower() == kl,
            lambda f: kl in (f.get("label") or "").lower()
            or kl in (f.get("name") or "").lower()
            or kl in (f.get("id") or "").lower()
            or kl in (f.get("text") or "").lower(),
        ):
            for f in fields:
                if score(f):
                    return f
        return None

    def fill_form(self, mapping: dict[str, Any],
                  submit: dict | None = None) -> dict:
        """Fill fields matched by label/name/id; optional submit click.

        submit: {"role": ..., "name": ...} snapshot match, or
        {"selector": css} direct click.
        """
        fields = self.describe_form()
        filled, failed = [], []
        for key, value in mapping.items():
            f = self._match_field(fields, key)
            if f is None:
                failed.append(key)
                continue
            ref = self.ref_for_selector(f["selector"])
            try:
                if ref:
                    r = self.act("fill", ref=ref, text=str(value))
                    ok = not r.get("failed")
                else:
                    r = self.js("set_value", f["selector"], value)
                    ok = bool(isinstance(r, dict) and r.get("ok"))
                (filled if ok else failed).append(key)
            except Exception:
                failed.append(key)
        clicked = None
        if submit:
            if "selector" in submit:
                r = self.js("click_sel", submit["selector"])
                clicked = bool(isinstance(r, dict) and r.get("ok"))
            else:
                ref = self.find_ref(submit.get("role"), submit.get("name"))
                if ref is None:
                    raise WfError(f"fill_form: submit target not found: {submit}")
                self.act("click", ref=ref)
                clicked = ref
        return {"filled": filled, "failed": failed, "submitClicked": clicked}

    def click_name(self, role: str, name: str) -> dict:
        ref = self.find_ref(role, name)
        if ref is None:
            raise WfError(f"click_name: no {role!r} matching {name!r}")
        return self.act("click", ref=ref)

    def download_by_text(self, link_text: str, out_dir: str,
                         timeout: float = 30.0) -> dict:
        ref = self.find_ref("link", link_text)
        if ref is None:
            raise WfError(f"download_by_text: no link matching {link_text!r}")
        return self.download(ref, out_dir, timeout)

    def paginate(self, url: str, item_selector: str,
                 next_selector: str, max_pages: int = 10,
                 settle_secs: float = 1.0) -> dict:
        """Collect extract_list items across pages via a next control."""
        self.navigate(url)
        seen: dict[str, dict] = {}
        pages = 0
        for _ in range(max(1, max_pages)):
            for it in self.extract_list(item_selector):
                seen[f"{it.get('href')}|{it.get('text')}"] = it
            pages += 1
            nxt = self.ref_for_selector(next_selector)
            try:
                if nxt:
                    self.act("click", ref=nxt)
                else:
                    r = self.js("click_sel", next_selector)
                    if not (isinstance(r, dict) and r.get("ok")):
                        break
            except Exception:
                break
            time.sleep(settle_secs)
        items = list(seen.values())
        return {"pages": pages, "count": len(items), "items": items}

    # -- composition ----------------------------------------------------------

    def call(self, wf_name: str, inputs: dict | None = None) -> Any:
        """Run a registry workflow as a logged sub-call (cycle-checked)."""
        from .workflows import _redact, _resolve, _run_inner

        try:
            _spec = _resolve(wf_name)[2].get("inputs", {})
        except Exception:
            _spec = {}

        st = self.state
        if wf_name in st.stack:
            raise WfCycleError(
                f"cycle: {' -> '.join([*st.stack, wf_name])}")
        if len(st.stack) >= st.max_depth:
            raise WfBudgetError(
                f"run {st.owner!r}: max depth {st.max_depth} exceeded "
                f"at {wf_name!r}")
        node: dict[str, Any] = {"wf": wf_name,
                                "inputs": _redact(inputs or {}, _spec),
                                "children": []}
        (self._node["children"] if self._node is not None
         else st.tree).append(node)
        t0 = time.monotonic()
        st.stack.append(wf_name)
        try:
            out = _run_inner(wf_name, inputs or {}, self.profile,
                             self.tab, st, node)
            node["ok"] = True
            return out
        except Exception as e:
            node["ok"] = False
            node["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            node["durationMs"] = round((time.monotonic() - t0) * 1000)
            st.stack.pop()
