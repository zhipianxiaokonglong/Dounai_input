"""The zero-build web console is shipped with the package and stays self-contained."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


def test_index_is_served_with_strict_csp(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "豆奶输入层" in response.text
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp


def test_static_assets_are_served(client) -> None:
    css = client.get("/assets/app.css")
    js = client.get("/assets/app.js")
    icon = client.get("/assets/favicon.svg")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    assert icon.status_code == 200
    assert "svg" in icon.headers["content-type"]
    assert "http://" not in js.text
    assert "https://" not in js.text


def test_every_dom_id_used_by_javascript_exists() -> None:
    web = Path(__file__).resolve().parents[1] / "src" / "dounai_input" / "web"
    html = (web / "index.html").read_text(encoding="utf-8")
    javascript = (web / "app.js").read_text(encoding="utf-8")
    used = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', javascript))
    declared = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
    assert used - declared == set()
    assert "onclick=" not in html
    assert "style=" not in html
    # API base URLs are inert placeholder text, not resources loaded by the page.
    assert '<script src="http' not in html
    assert '<link rel="stylesheet" href="http' not in html


def test_javascript_parses_when_node_is_available() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed on this runner")
    web = Path(__file__).resolve().parents[1] / "src" / "dounai_input" / "web" / "app.js"
    result = subprocess.run(
        [node, "--check", str(web)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_privacy_policy_is_packaged() -> None:
    from importlib.resources import files

    text = files("dounai_input").joinpath("resources/privacy_policy_zh.md").read_text("utf-8")
    assert "生物特征" in text
    assert "不把声纹模板" in text
