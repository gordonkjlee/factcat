"""Tests for the Pages builder. No network."""

from __future__ import annotations

import re
from html import escape as html_escape
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
    assert "An open-source alternative to Amplitude and Mixpanel" in index
    assert "no SDK, no ingestion, nothing hosted." in index
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
    assert pitch == build_pages.PITCH  # README lede and fallback must not drift
    assert rest.startswith("<img ")
    assert "# Factcat" not in rest.splitlines()[0]
    assert "## The problem" in rest


def test_split_readme_joins_multiline_lede():
    text = (
        "# Factcat\n\n"
        '<img src="x.png">\n\n'
        "First line of the pitch\n"
        "continues on a second line.\n\n"
        "## The problem\n\nBody.\n"
    )
    pitch, rest = build_pages.split_readme(text)
    assert pitch == "First line of the pitch continues on a second line."
    assert "continues on a second line." not in rest
    assert "## The problem" in rest


def test_cname_must_match_domain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(build_pages, "ROOT", tmp_path)
    (tmp_path / "CNAME").write_text("example.com\n", encoding="utf-8")
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(SystemExit, match="CNAME must be"):
        build_pages.write_cname(dest)


def test_every_in_page_anchor_resolves_to_a_heading():
    """An in-page link works on GitHub whatever we do, because GitHub slugs
    headings itself, and is dead on the site unless `toc` gives them ids. The
    README's first such link shipped broken for exactly that reason: the
    existing anchor test only checks that rewriting leaves them alone, never
    that the target exists.

    Mutation: drop "toc" from MARKDOWN_EXTENSIONS.
    """
    _pitch, rest = build_pages.split_readme((ROOT / "README.md").read_text(encoding="utf-8"))
    html = build_pages.render_markdown(rest)
    ids = set(re.findall(r'id="([^"]+)"', html))
    links = re.findall(r'href="#([^"]+)"', html)
    assert links, "no in-page anchors left; drop this guard or the link it protects"
    assert [link for link in links if link not in ids] == []


def test_the_hero_command_is_the_one_the_readme_prints():
    """The landing hero hard-coded its own `pip install factcat`, directly
    above whatever the README said. They disagreed, and the hero's form
    installs no warehouse driver - so the site's most prominent command left
    the reader unable to connect to anything.

    Mutation: return the fallback instead of reading the README.
    """
    _pitch, rest = build_pages.split_readme((ROOT / "README.md").read_text(encoding="utf-8"))
    command = build_pages.first_install_command(rest)
    assert command in rest, "the hero prints a command the README does not"
    assert command.startswith("pip install ")
    html = build_pages.wrap_html(
        title="t", description="d", canonical="/", body="", install=command
    )
    assert f"<code>{html_escape(command)}</code>" in html
