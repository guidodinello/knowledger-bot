import pytest

from knowledger.youtube import (
    _extract_og_title,
    build_doc_name,
    fetch_video_metadata,
    has_date_suffix,
    watch_url,
)


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


def test_incomplete_oembed_metadata_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class IncompleteResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, str]:
            return {"title": "A title without an author"}

    monkeypatch.setattr(
        "knowledger.youtube.requests.get",
        lambda *args, **kwargs: IncompleteResponse(),
    )

    with pytest.raises(ValueError, match="incomplete video metadata"):
        fetch_video_metadata("https://youtu.be/abc123")


def test_doc_name_carries_a_date_suffix_when_the_upload_date_is_known() -> None:
    dated = build_doc_name("On-Chain Mind", "Capitulating", "2026-08-04")
    assert dated == "Youtube - On-Chain Mind - Capitulating - 2026-08-04"
    assert has_date_suffix(dated)


def test_doc_name_without_an_upload_date_is_detectable_as_degraded() -> None:
    """The dateless form is what the interactive flow falls back to when it can\'t
    reach the watch page — and what makes its name diverge from the poller\'s."""
    assert not has_date_suffix(build_doc_name("On-Chain Mind", "Capitulating", None))


def test_a_title_ending_in_digits_is_not_mistaken_for_a_date_suffix() -> None:
    assert not has_date_suffix(build_doc_name("Ch", "Episode - 42", None))


def test_watch_url_round_trips_through_extract_video_id() -> None:
    from knowledger.youtube import extract_video_id

    assert extract_video_id(watch_url("BSFH8tFR2-k")) == "BSFH8tFR2-k"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
