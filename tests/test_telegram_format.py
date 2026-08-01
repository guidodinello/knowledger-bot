"""The formatting primitives are the reason the escaping bug class (see
docs/bugs/unescaped-markdown-injection.md, docs/bugs/inqueue-markdown-italics.md) can't
recur, so their edge cases are worth pinning down directly."""

from knowledger.history import UploadRecord, load_history
from knowledger.persistence import atomic_write_json
from knowledger.telegram_format import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    cap_entries,
    cap_message,
    esc,
    subject,
    truncate,
    video_link,
)


def test_esc_neutralizes_every_character_telegram_parses() -> None:
    assert esc("Tech & <b>Bold</b>") == "Tech &amp; &lt;b&gt;Bold&lt;/b&gt;"


def test_esc_leaves_markdown_active_characters_alone() -> None:
    """Under HTML these are ordinary text. The whole point of the switch is that a title
    like `AI_Explained` needs no special handling at the call site."""
    assert esc("AI_Explained *now* `here` [link]") == "AI_Explained *now* `here` [link]"


def test_video_link_without_an_id_degrades_to_plain_text() -> None:
    """Upload-history records written before UploadRecord gained video_id have None."""
    assert video_link("A & B", None) == "A &amp; B"


def test_video_link_escapes_the_title_inside_the_anchor() -> None:
    assert video_link("A & B", "abc123") == '<a href="https://youtu.be/abc123">A &amp; B</a>'


def test_subject_omits_the_channel_when_there_isnt_one() -> None:
    assert subject("Title", None, None) == "Title"
    assert subject("Title", "", None) == "Title"
    assert subject("Title", "Chan", None) == "Title — Chan"


def test_truncate_runs_before_escaping_so_entities_are_never_sliced() -> None:
    """`&` is one character to truncate and five to Telegram. Cutting the escaped form
    could leave a bare `&amp` and break the message, so truncate must see raw text."""
    assert video_link("&" * 100, "v1").count("&amp;") == 69  # 69 kept + the … marker


def test_cap_message_leaves_a_short_message_untouched() -> None:
    assert cap_message("hello") == "hello"


def test_cap_message_never_severs_an_anchor_tag() -> None:
    """A byte-offset slice can cut through `<a href="...">`, which makes Telegram reject
    the entire message — trading "truncated but readable" for "nothing arrives"."""
    line = '<a href="https://youtu.be/aaaaaaaaaaa">' + "x" * 60 + "</a>"
    text = "\n".join([line] * 200)
    assert len(text) > TELEGRAM_MAX_MESSAGE_LENGTH

    capped = cap_message(text)

    assert len(capped) <= TELEGRAM_MAX_MESSAGE_LENGTH
    assert capped.count("<a ") == capped.count("</a>")
    assert capped.endswith("… (truncated)")


def test_cap_message_falls_back_to_plain_text_for_one_enormous_line() -> None:
    """Degenerate case: nothing can be kept whole. Stripping tags keeps the result valid
    HTML at any cut point rather than dropping the message entirely."""
    capped = cap_message('<a href="https://x/">' + "y" * 6000 + "</a>")

    assert len(capped) <= TELEGRAM_MAX_MESSAGE_LENGTH
    assert "<a" not in capped
    assert capped.endswith("… (truncated)")


def test_cap_entries_reports_what_it_dropped() -> None:
    entries = [[f"• {i}", f"  detail {i}"] for i in range(5)]

    assert cap_entries(entries, 2) == ["• 0", "  detail 0", "• 1", "  detail 1", "+3 more"]
    assert cap_entries(entries, 10) == [line for entry in entries for line in entry]


def test_truncate_marks_that_it_shortened() -> None:
    assert truncate("abc", 10) == "abc"
    assert truncate("a" * 20, 10) == "a" * 9 + "…"


def test_history_still_loads_records_written_before_video_id_existed(tmp_path) -> None:
    """video_id is defaulted rather than required precisely so the existing
    upload_history.json keeps loading without a schema-version bump."""
    atomic_write_json(
        tmp_path / "upload_history.json",
        {
            "version": 1,
            "records": [
                {
                    "project_id": "p",
                    "file_name": "f",
                    "video_title": "T",
                    "channel_name": "C",
                    "uploaded_at": "2026-07-30T00:00:00+00:00",
                },
            ],
        },
    )

    records = load_history(tmp_path)

    assert records == [
        UploadRecord(
            project_id="p",
            file_name="f",
            video_title="T",
            channel_name="C",
            uploaded_at="2026-07-30T00:00:00+00:00",
            video_id=None,
        ),
    ]
