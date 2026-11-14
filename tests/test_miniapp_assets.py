"""Keep GitHub Pages packaging and browser script order in sync with HTML."""

from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.files = []
        self.ids = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        url = attrs.get("src") if tag == "script" else attrs.get("href") if tag == "link" else None
        if url and url.startswith("./"):
            self.files.append(url.removeprefix("./").split("?", 1)[0])


def test_all_referenced_assets_are_published_and_ids_unique():
    html = Assets()
    html.feed((ROOT / "miniapp/index.html").read_text())
    workflow = (ROOT / ".github/workflows/miniapp-pages.yml").read_text()
    for filename in html.files:
        assert (ROOT / "miniapp" / filename).is_file()
        assert f"cp miniapp/{filename} _site/" in workflow
    assert len(html.ids) == len(set(html.ids))
    assert html.files.index("client.js") < html.files.index("api.js")
    assert html.files.index("client.js") < html.files.index("correction-editor.js")
    assert html.files.index("client.js") < html.files.index("management.js")
