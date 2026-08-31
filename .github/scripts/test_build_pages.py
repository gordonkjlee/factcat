"""Tests for the Pages builder. No network."""

from __future__ import annotations

from pathlib import Path

import pytest

import build_pages

ROOT = Path(__file__).resolve().parents[2]


def test_rewrite_setup_guide_to_published_page():
    src = ROOT / "README.md"
    assert (
        build_pages.rewrite_url(
            "packages/engine/factcat_app/guides/setup-bigquery.md", src
        )
        == "setup-bigquery.html"
    )


def test_rewrite_source_file_to_github():
    src = ROOT / "README.md"
    assert (
        build_pages.rewrite_url("packages/engine/factcat/dialects.py", src)
        == "https://github.com/gordonkjlee/factcat/blob/main/packages/engine/factcat/dialects.py"
    )


def test_rewrite_leaves_external_and_anchors():
    src = ROOT / "README.md"
    assert build_pages.rewrite_url("https://pypi.org/project/factcat/", src) == (
        "https://pypi.org/project/factcat/"
    )
    assert build_pages.rewrite_url("#install", src) == "#install"


def test_builds_site_from_readme(tmp_path: Path):
    site = build_pages.build(tmp_path)

    index = (site / "index.html").read_text(encoding="utf-8")
    assert "<title>Factcat</title>" in index
    assert "Open-source, warehouse-first product analytics." in index
    assert "pip install factcat" in index
    assert "https://github.com/gordonkjlee/factcat" in index
    assert "https://pypi.org/project/factcat/" in index
    assert "The same definition, in Factcat" in index
    assert "<table>" in index
    assert "packages/engine/factcat_app/guides/setup-bigquery.md" not in index
    assert "setup-bigquery.html" in index
    assert (
        "https://github.com/gordonkjlee/factcat/blob/main/packages/engine/factcat/dialects.py"
        in index
    )
    assert 'src="assets/waiting.jpg"' in index

    guide = (site / "setup-bigquery.html").read_text(encoding="utf-8")
    assert "Map **one wide events table**" not in guide
    assert "one wide events table" in guide
    assert "pip install factcat" in guide

    assert (site / "CNAME").read_text(encoding="utf-8") == "factcat.dev\n"
    assert (site / ".nojekyll").is_file()
    assert (site / "assets" / "waiting.jpg").is_file()
    assert (site / "assets" / "logo.png").is_file()


def test_split_readme_uses_lede_and_keeps_image():
    pitch, rest = build_pages.split_readme((ROOT / "README.md").read_text(encoding="utf-8"))
    assert pitch == "Open-source, warehouse-first product analytics."
    assert rest.startswith("<img ")
    assert "# Factcat" not in rest.splitlines()[0]
    assert "## The problem" in rest


def test_cname_must_match_domain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(build_pages, "ROOT", tmp_path)
    (tmp_path / "CNAME").write_text("example.com\n", encoding="utf-8")
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(SystemExit, match="CNAME must be"):
        build_pages.write_cname(dest)
