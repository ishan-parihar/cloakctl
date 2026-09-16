"""Extract a list of {text, href} items from one page."""

META = {
    "version": "1.0.0",
    "description": "Extract text+href items matching a selector on one page.",
    "site": "",
    "inputs": {
        "url": {"type": "str", "required": True,
                "description": "Page URL."},
        "selector": {"type": "str", "required": True,
                     "description": "Item CSS selector."},
    },
    "depends_on": [],
}


def run(ctx, inputs):
    ctx.navigate(inputs["url"])
    items = ctx.extract_list(inputs["selector"])
    return {"count": len(items), "items": items}
