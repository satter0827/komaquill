"""Verify documentation contracts within the declared test scope."""

from tools.checks import documentation as check_docs


def test_visible_headings_keep_japanese_and_duplicate_anchor_order():
    assert check_docs.anchors(
        "## *日本語* `見出し`\n## 日本語 見出し\n\nOther title\n---\n```\n# Hidden\n```\n"
    ) == {"日本語-見出し", "日本語-見出し-1", "other-title"}
