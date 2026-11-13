"""Read-only, escaped reader for this sample's small Markdown setup subset."""

import re
from html import escape
from pathlib import Path


def inline(text):
    text = escape(text)
    text = re.sub(r"\[([^\]]+)\]\((https://[^)\s]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", text)


def setup_page():
    source = Path(__file__).with_name("README.md").read_text().split("## Setup\n")[1].split("\n## Recovery")[0]
    fenced = False
    body = []
    for line in source.splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
            body.append("<pre><code>" if fenced else "</code></pre>")
        elif fenced:
            body.append(escape(line))
        elif line.strip().startswith("|"):
            cells = [cell.strip() for cell in line.strip().split("|")[1:-1]]
            if cells[0] not in ("Name", "Variable") and not re.fullmatch("-+", cells[0]):
                body.append(f"<p><strong>{inline(cells[0])}</strong>: {inline(cells[1])}</p>")
        elif step := re.match(r"^(\d+)\. \*\*(.+?)\*\*\s*(.*)$", line):
            body.append(f"<h2>{step[1]}. {inline(step[2])}</h2><p>{inline(step[3])}</p>")
        elif line.strip():
            body.append(f"<p>{inline(line.strip())}</p>")
    return (
        '<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        '<title>Weather setup</title><link rel="stylesheet" href="/static/style.css">'
        '<body class="setup-reader"><a href="./">Back to sample</a>'
        '<h1 id="setup">Connect your already deployed app</h1>'
        '<p>Manual setup · development/test only. Obtain resource-owner approval before cloud actions.</p>'
        + "\n".join(body) +
        '<p><a href="https://github.com/AzureAppService/app-service-builder-key-vault">'
        'Full README, recovery and optional local details (upstream)</a></p></body></html>'
    )
