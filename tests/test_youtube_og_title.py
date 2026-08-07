import pytest

from knowledger.youtube import (
    _extract_og_title,
    build_doc_name,
    fetch_video_metadata,
    is_undated_doc_name,
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
    assert not is_undated_doc_name(dated, "On-Chain Mind", "Capitulating")


def test_doc_name_without_an_upload_date_is_detectable_as_degraded() -> None:
    """The dateless form is what the interactive flow falls back to when it can\'t
    reach the watch page — and what makes its name diverge from the poller\'s."""
    undated = build_doc_name("On-Chain Mind", "Capitulating", None)
    assert is_undated_doc_name(undated, "On-Chain Mind", "Capitulating")


def test_a_title_that_itself_ends_in_a_date_is_still_read_as_undated() -> None:
    """The case a trailing-date regex gets wrong. `Mercados - 2026-08-04` builds the
    dateless `Youtube - Ch - Mercados - 2026-08-04`, indistinguishable by shape from a
    dated name — and these are exactly the news/markets channels being watched, so
    reading it as already-dated would skip the re-resolution it needs."""
    title = "Mercados - 2026-08-04"
    undated = build_doc_name("Ch", title, None)
    assert undated == "Youtube - Ch - Mercados - 2026-08-04"
    assert is_undated_doc_name(undated, "Ch", title)
    assert not is_undated_doc_name(build_doc_name("Ch", title, "2026-08-05"), "Ch", title)


def test_a_name_for_a_different_video_is_not_read_as_this_one_undated() -> None:
    assert not is_undated_doc_name("Youtube - Ch - Other", "Ch", "Capitulating")


def test_watch_url_round_trips_through_extract_video_id() -> None:
    from knowledger.youtube import extract_video_id

    assert extract_video_id(watch_url("BSFH8tFR2-k")) == "BSFH8tFR2-k"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
