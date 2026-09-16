"""Click a download link by its text and save the file."""

META = {
    "version": "1.0.0",
    "description": "Download the file behind a link, matched by link text.",
    "site": "",
    "inputs": {
        "url": {"type": "str", "required": True,
                "description": "Page URL."},
        "link_text": {"type": "str", "required": True,
                      "description": "Link text to match."},
        "out_dir": {"type": "str", "required": True,
                    "description": "Destination directory."},
    },
    "depends_on": [],
}


def run(ctx, inputs):
    ctx.navigate(inputs["url"])
    res = ctx.download_by_text(inputs["link_text"], inputs["out_dir"])
    return {"path": res.get("path"), "bytes": res.get("bytes")}
