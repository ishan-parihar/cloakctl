"""Versioned in-page JS functions (the data plane).

Each entry is a self-contained expression evaluated in the page via
`run`/`exec`. Python (the control plane) never duplicates DOM logic here —
it ships these functions and branches on the JSON they return.
"""

PAGEFNS_VERSION = 1

_DESCRIBE_FORM = """(() => {
  function css(el) {
    if (el.id) return '#' + el.id;
    if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
    const parts = [];
    let n = el;
    while (n && n.nodeType === 1 && parts.length < 4) {
      let s = n.tagName.toLowerCase();
      const sibs = [...(n.parentElement ? n.parentElement.children : [])]
        .filter(e => e.tagName === n.tagName);
      if (sibs.length > 1) s += ':nth-of-type(' + (sibs.indexOf(n) + 1) + ')';
      parts.unshift(s);
      n = n.parentElement;
    }
    return parts.join(' > ');
  }
  function labelFor(el) {
    if (el.id) {
      const l = document.querySelector('label[for="' + el.id + '"]');
      if (l) return l.innerText.trim().slice(0, 80);
    }
    const wrap = el.closest('label');
    if (wrap) return wrap.innerText.trim().slice(0, 80);
    return '';
  }
  return [...document.querySelectorAll('input,select,textarea,button')].map(el => ({
    tag: el.tagName.toLowerCase(),
    type: (el.type || '').toLowerCase(),
    name: el.name || '', id: el.id || '',
    label: labelFor(el),
    text: (el.innerText || '').trim().slice(0, 60),
    selector: css(el),
  }));
})()"""

_SET_VALUE = """(sel, v) => {
  const el = document.querySelector(sel);
  if (!el) return {ok: false, error: 'no element: ' + sel};
  el.focus();
  if (el.tagName === 'SELECT') {
    el.value = v;
  } else if (el.type === 'checkbox' || el.type === 'radio') {
    const want = (v === true || v === 'true' || v === 'checked' || v === '1');
    if (el.checked !== want) el.click();
    return {ok: true, checked: el.checked};
  } else {
    el.value = v;
  }
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return {ok: true, value: el.value};
}"""

_CLICK_SEL = """(sel) => {
  const el = document.querySelector(sel);
  if (!el) return {ok: false, error: 'no element: ' + sel};
  el.scrollIntoView({block: 'center'});
  el.click();
  return {ok: true};
}"""

_EXTRACT_TABLE = """(sel) => {
  const t = sel ? document.querySelector(sel) : document.querySelector('table');
  if (!t) return {ok: false, error: 'no table' + (sel ? ': ' + sel : '')};
  const rows = [...t.querySelectorAll('tr')].map(tr =>
    [...tr.querySelectorAll('th,td')].map(c => c.innerText.trim().slice(0, 500)));
  const head = t.querySelectorAll('th').length
    ? rows[0] : [];
  return {ok: true, headers: head,
          rows: head.length ? rows.slice(1) : rows,
          count: head.length ? rows.length - 1 : rows.length};
}"""

_EXTRACT_LIST = """(sel) => {
  const els = [...document.querySelectorAll(sel)];
  return els.slice(0, 500).map(el => ({
    text: (el.innerText || '').trim().slice(0, 200),
    href: el.href || '',
  }));
}"""

_SCROLL_COLLECT = """async (itemSel, maxItems, maxScrolls) => {
  const seen = new Map();
  for (let i = 0; i < maxScrolls && seen.size < maxItems; i++) {
    for (const el of document.querySelectorAll(itemSel)) {
      const text = (el.innerText || '').trim().slice(0, 200);
      const key = (el.href || '') + '|' + text;
      if (text && !seen.has(key)) seen.set(key, {text, href: el.href || ''});
      if (seen.size >= maxItems) break;
    }
    const before = document.body.scrollHeight;
    window.scrollTo(0, before);
    await new Promise(r => setTimeout(r, 600));
    if (document.body.scrollHeight === before) break;
  }
  return [...seen.values()];
}"""

_DESCRIBE_PAGE = """(() => ({
  url: location.href, title: document.title,
  headings: [...document.querySelectorAll('h1,h2,h3')].slice(0, 20)
    .map(h => h.innerText.trim().slice(0, 120)),
  linkCount: document.querySelectorAll('a[href]').length,
  formCount: document.querySelectorAll('form').length,
}))()"""

FUNCS = {
    "describe_form": _DESCRIBE_FORM,
    "set_value": _SET_VALUE,
    "click_sel": _CLICK_SEL,
    "extract_table": _EXTRACT_TABLE,
    "extract_list": _EXTRACT_LIST,
    "scroll_collect": _SCROLL_COLLECT,
    "describe_page": _DESCRIBE_PAGE,
}


def get(name: str) -> str:
    try:
        return FUNCS[name]
    except KeyError:
        raise KeyError(f"pagefn {name!r} unknown; have: {sorted(FUNCS)}")
