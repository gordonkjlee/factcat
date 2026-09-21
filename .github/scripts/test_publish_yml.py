"""The publish workflow refuses a tag that is not on main. No network."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEXT = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")


def test_checkout_of_the_tag_is_full_history():
    """Mutation: drop fetch-depth on the tag checkout (not the lag job) and this goes red."""
    idx = TEXT.index("ref: ${{ steps.release.outputs.tag }}")
    window = TEXT[idx : idx + 160]
    assert "fetch-depth: 0" in window


def test_refuses_a_tag_that_is_not_an_ancestor_of_main():
    """Mutation: drop merge-base --is-ancestor and this goes red."""
    assert "git merge-base --is-ancestor" in TEXT
    assert "origin/main" in TEXT
