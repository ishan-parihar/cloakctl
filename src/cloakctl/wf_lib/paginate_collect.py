"""Paginated collection, composed over scrape_list (compounding demo)."""

META = {
    "version": "1.0.0",
    "description": "Collect items across paginated pages by composing "
                   "scrape_list per page.",
    "site": "",
    "inputs": {
        "url": {"type": "str", "required": True,
                "description": "First page URL."},
        "selector": {"type": "str", "required": True,
                     "description": "Item CSS selector."},
        "next_selector": {"type": "str", "required": True,
                          "description": "Next-page control CSS selector."},
        "max_pages": {"type": "int", "required": False, "default": 5,
                      "description": "Page bound (default 5)."},
    },
    "depends_on": ["scrape_list"],
}


def run(ctx, inputs):
    seen = {}
    ctx.navigate(inputs["url"])
    pages = 0
    for _ in range(inputs.get("max_pages", 5)):
        page = ctx.call("scrape_list", {"url": ctx.describe_page().get("url", ""),
                                        "selector": inputs["selector"]})
        for it in page["items"]:
            seen[f"{it.get('href')}|{it.get('text')}"] = it
        pages += 1
        try:
            r = ctx.js("click_sel", inputs["next_selector"])
            if not (isinstance(r, dict) and r.get("ok")):
                break
        except Exception:
            break
        import time as _t  # settle for client-rendered next pages
        _t.sleep(1.0)
    items = list(seen.values())
    return {"pages": pages, "count": len(items), "items": items}
