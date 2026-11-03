"""Статические проверки приложения: файлы оболочки существуют, CSP не нарушается, innerHTML не используется."""
import json
import re
from pathlib import Path

DOCS = Path(__file__).parent.parent / "docs"


def test_service_worker_shell_files_exist():
    sw = (DOCS / "sw.js").read_text(encoding="utf-8")
    shell = re.search(r"const SHELL = \[(.*?)\];", sw, re.S).group(1)
    names = re.findall(r'"([^"]+)"', shell)
    assert "index.html" in names and "app.js" in names
    for name in names:
        if name != "./":
            assert (DOCS / name).exists(), name


def test_index_references_existing_files_and_has_strict_csp():
    html = (DOCS / "index.html").read_text(encoding="utf-8")
    for ref in re.findall(r'(?:href|src)="([^"#]+)"', html):
        if not ref.startswith("http"):
            assert (DOCS / ref).exists(), ref
    csp = re.search(r'Content-Security-Policy" content="([^"]+)"', html).group(1)
    assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp and "default-src 'none'" in csp
    assert "<script>" not in html and ' style="' not in html and " onclick=" not in html


def test_no_dangerous_dom_apis_in_app():
    app = (DOCS / "app.js").read_text(encoding="utf-8")
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert bad not in app, bad
    assert 'setAttribute("style"' not in app and ".style." not in app


def test_manifest_and_icons():
    m = json.loads((DOCS / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert m["start_url"] == "./" and m["scope"] == "./" and m["display"] == "standalone"
    for icon in m["icons"]:
        assert (DOCS / icon["src"]).exists()
    assert (DOCS / "icons" / "badge-96.png").exists() and (DOCS / "icons" / "apple-touch-icon.png").exists()


def test_service_worker_opens_only_safe_links():
    sw = (DOCS / "sw.js").read_text(encoding="utf-8")
    assert 'u.protocol === "https:"' in sw and "safeUrl(" in sw
