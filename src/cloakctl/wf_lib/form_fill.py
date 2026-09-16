"""Semantic form fill: match fields by label/name/id, fill, submit."""

META = {
    "version": "1.0.0",
    "description": "Fill a form by field label/name (no snapshot refs needed) "
                   "and optionally click submit.",
    "site": "",
    "inputs": {
        "url": {"type": "str", "required": True,
                "description": "Form page URL."},
        "fields": {"type": "dict", "required": True,
                   "description": "Map of field label/name -> value."},
        "submit_role": {"type": "str", "required": False,
                        "description": "Submit button role (default: button)."},
        "submit_name": {"type": "str", "required": False,
                        "description": "Submit button name text."},
        "expect_text": {"type": "str", "required": False,
                        "description": "Wait for this text after submit."},
    },
    "depends_on": [],
}


def run(ctx, inputs):
    ctx.navigate(inputs["url"])
    submit = None
    if inputs.get("submit_name"):
        submit = {"role": inputs.get("submit_role") or "button",
                  "name": inputs["submit_name"]}
    res = ctx.fill_form(inputs["fields"], submit=submit)
    if inputs.get("expect_text"):
        try:
            w = ctx.wait(text=inputs["expect_text"], timeout=15.0)
            res["waitMatched"] = w.get("matched")
        except Exception as e:
            res["waitMatched"] = f"timeout: {e}"
    if res["failed"]:
        raise RuntimeError(f"form_fill: unfilled fields: {res['failed']}")
    res["url"] = ctx.describe_page().get("url", "")
    return res
