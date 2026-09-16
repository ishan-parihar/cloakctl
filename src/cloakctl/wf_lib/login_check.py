"""Classify the live session: logged_in | anonymous | challenged | burn."""

META = {
    "version": "1.0.0",
    "description": "Probe the live profile session and classify it "
                   "(logged_in | anonymous | challenged | burn_signature).",
    "site": "",
    "inputs": {
        "url": {"type": "str", "required": False,
                "description": "Probe URL (default: cloakctl probe)."},
    },
    "depends_on": [],
}


def run(ctx, inputs):
    url = inputs.get("url")
    if url:
        ctx.navigate(url)
    res = ctx.validate(url)
    return {"verdict": res.get("verdict"), "probe": res.get("probe"),
            "profile": res.get("profile")}
