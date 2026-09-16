"""Extract a table's headers + rows as structured data."""

META = {
    "version": "1.0.0",
    "description": "Extract an HTML table into headers + rows.",
    "site": "",
    "inputs": {
        "url": {"type": "str", "required": True,
                "description": "Page URL."},
        "selector": {"type": "str", "required": False,
                     "description": "Table CSS selector (default: first table)."},
    },
    "depends_on": [],
}


def run(ctx, inputs):
    ctx.navigate(inputs["url"])
    res = ctx.extract_table(inputs.get("selector"))
    return {"headers": res.get("headers", []), "rows": res.get("rows", []),
            "count": res.get("count", 0)}
