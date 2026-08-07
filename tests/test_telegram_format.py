"""The formatting primitives are the reason the escaping bug class (see
docs/bugs/unescaped-markdown-injection.md, docs/bugs/inqueue-markdown-italics.md) can't
recur, so their edge cases are worth pinning down directly."""

from knowledger.history import UploadRecord, load_history
from knowledger.persistence import atomic_write_json
from knowledger.telegram_format import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    blockquote,
    cap_entries,
    cap_message,
    cap_plain_message,
    esc,
    subject,
    tg_len,
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


def test_cap_message_never_splits_an_entity_in_one_enormous_line() -> None:
    capped = cap_message("<b>" + "&" * 5000 + "</b>")
    escaped_prefix = capped.removesuffix("\n… (truncated)")

    assert len(capped) <= TELEGRAM_MAX_MESSAGE_LENGTH
    assert escaped_prefix.endswith("&amp;")
    assert escaped_prefix.count("&") == escaped_prefix.count("&amp;")


def test_blockquote_spans_the_whole_block_rather_than_wrapping_each_line() -> None:
    assert blockquote(["a", "b", "c"]) == ["<blockquote>a", "b", "c</blockquote>"]
    assert blockquote(["only"]) == ["<blockquote>only</blockquote>"]
    assert blockquote([]) == []


def test_cap_message_closes_a_blockquote_that_truncation_cut_open() -> None:
    """Line-dropping used to be safe only because every tag opened and closed on one
    line. A section-spanning blockquote breaks that, and Telegram rejects the entire
    message for one unbalanced tag — the exact "nothing arrives at all" failure the
    line-oriented truncation exists to avoid."""
    text = "\n".join(blockquote([f"• entry {i}" for i in range(500)]))
    assert len(text) > TELEGRAM_MAX_MESSAGE_LENGTH

    capped = cap_message(text)

    assert len(capped) <= TELEGRAM_MAX_MESSAGE_LENGTH
    assert capped.count("<blockquote>") == capped.count("</blockquote>") == 1
    # Closed at the cut, before the trailer — not left hanging open.
    assert capped.endswith("</blockquote>\n… (truncated)")


def test_cap_message_closes_a_blockquote_of_links_and_italics_that_truncation_cut_open() -> None:
    """The bare-blockquote test above never exercises the closer against interleaved
    `<a>`/`<i>` tags — the actual /inqueue shape. Regression coverage for the real
    markup, not a simplified stand-in."""
    entries = [
        f'<a href="https://youtu.be/v{i}">Video {i}</a> — <i>seen just now</i>' for i in range(500)
    ]
    text = "\n".join(blockquote(entries))
    assert len(text) > TELEGRAM_MAX_MESSAGE_LENGTH

    capped = cap_message(text)

    assert len(capped) <= TELEGRAM_MAX_MESSAGE_LENGTH
    assert capped.count("<blockquote>") == capped.count("</blockquote>") == 1
    assert capped.count("<a ") == capped.count("</a>")
    assert capped.count("<i>") == capped.count("</i>")


# --- UTF-16 accounting -------------------------------------------------------------
#
# Telegram measures its 4096 limit in UTF-16 code units. Every astral-plane character
# is one Python character and two of those, so a len()-based check undercounts any
# message carrying emoji — and the bot's section headers are exactly that. A message
# passing at just under the cap was still rejected by the API as too long, which is
# the failure cap_message exists to prevent.


def test_tg_len_counts_astral_characters_as_two() -> None:
    assert tg_len("abc") == 3
    assert tg_len("\U0001f4ca") == 2  # 📊, one Python char
    assert tg_len("\U0001f4ca ok") == 5
    assert tg_len("é") == 1  # BMP, so one unit — not a bytes-length count


def test_cap_message_caps_a_message_that_only_overflows_in_utf16_units() -> None:
    """Under the cap by len(), over it by what Telegram counts. This is the whole bug:
    the message sails past the check and the API rejects it."""
    line = "\U0001f4ca" + "a" * 9  # 10 Python characters, 11 UTF-16 units
    text = "\n".join([line] * 372)  # 4091 characters, 4463 units

    assert len(text) < TELEGRAM_MAX_MESSAGE_LENGTH
    assert tg_len(text) > TELEGRAM_MAX_MESSAGE_LENGTH

    capped = cap_message(text)

    assert tg_len(capped) <= TELEGRAM_MAX_MESSAGE_LENGTH
    assert capped.endswith("… (truncated)")


def test_cap_plain_message_caps_in_utf16_units_too() -> None:
    text = "\U0001f4ca" * 3000

    assert len(text) < TELEGRAM_MAX_MESSAGE_LENGTH
    assert tg_len(text) > TELEGRAM_MAX_MESSAGE_LENGTH

    capped = cap_plain_message(text)

    assert tg_len(capped) <= TELEGRAM_MAX_MESSAGE_LENGTH


def test_capping_never_splits_a_surrogate_pair() -> None:
    """The prefix is sliced by Python code point, so an astral character is either
    wholly kept or wholly dropped — a lone surrogate would be invalid UTF-8 on the
    wire and rejected before the length ever mattered."""
    capped = cap_plain_message("\U0001f4ca" * 3000)

    assert capped.encode("utf-8")  # would raise on a lone surrogate
    assert "\ud83d" not in capped


def test_one_enormous_astral_line_still_fits_after_the_plain_text_fallback() -> None:
    """The degenerate branch: a single line too long on its own, made of characters
    that cost two units each."""
    capped = cap_message("<b>" + "\U0001f4ca" * 5000 + "</b>")

    assert tg_len(capped) <= TELEGRAM_MAX_MESSAGE_LENGTH
    assert capped.encode("utf-8")


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
