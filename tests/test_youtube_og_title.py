import pytest

from knowledger.youtube import _extract_og_title


def test_extracts_title_in_document_order() -> None:
    html = '<html><head><meta property="og:title" content="Hello World"></head></html>'
    assert _extract_og_title(html) == "Hello World"


def test_reordered_attributes() -> None:
    html = '<meta content="Reordered Title" property="og:title">'
    assert _extract_og_title(html) == "Reordered Title"


def test_extra_attributes_between() -> None:
    html = '<meta name="og:title" property="og:title" content="Extra Attrs" data-x="1">'
    assert _extract_og_title(html) == "Extra Attrs"


def test_case_variation_in_tag_and_attrs() -> None:
    html = '<META PROPERTY="og:title" CONTENT="Upper Case Tag">'
    assert _extract_og_title(html) == "Upper Case Tag"


def test_escaped_entities_are_unescaped() -> None:
    html = '<meta property="og:title" content="Tom &amp; Jerry &#39;s Show">'
    assert _extract_og_title(html) == "Tom & Jerry 's Show"


def test_missing_tag_returns_none() -> None:
    html = "<html><head></head></html>"
    assert _extract_og_title(html) is None


def test_only_first_matching_tag_is_used() -> None:
    html = '<meta property="og:title" content="First"><meta property="og:title" content="Second">'
    assert _extract_og_title(html) == "First"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
