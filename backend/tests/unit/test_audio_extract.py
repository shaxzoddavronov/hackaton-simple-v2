"""Tests for the audio/video transcription branch of ``doc_extract``.

We never load the real faster-whisper model in unit tests — the wheel
download is large and CPU inference is slow. Instead each test
monkey-patches the ``_get_whisper`` factory (or the WhisperModel
class) with a stub that returns predetermined segments. This keeps
the test suite fast (< 1 s) while still exercising the dispatch
logic, transcript concatenation, error wrapping, and the
``SUPPORTED_EXTENSIONS`` contract.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.services import doc_extract
from app.services.doc_extract import (
    SUPPORTED_EXTENSIONS,
    _AUDIO_EXTS,
    _extract_audio,
    extract_text,
)


# ── helpers ───────────────────────────────────────────────────────


@dataclass
class _FakeSegment:
    """Stand-in for the ``faster_whisper.transcribe.Segment`` named
    tuple. Only the ``.text`` attribute is read by ``_extract_audio``.
    """

    text: str


@dataclass
class _FakeInfo:
    language: str = "en"
    language_probability: float = 0.99


class _FakeWhisperModel:
    """Stub that records ``transcribe()`` calls and returns canned
    segments. Each test sets ``segments`` before calling the extractor."""

    def __init__(self, segments: list[_FakeSegment] | None = None) -> None:
        self.segments = segments or []
        self.calls: list[str] = []

    def transcribe(self, path: str, language=None):  # noqa: D401
        self.calls.append(path)
        return iter(self.segments), _FakeInfo()


@pytest.fixture(autouse=True)
def _reset_whisper_singleton(monkeypatch: pytest.MonkeyPatch):
    """Make sure each test starts with a clean ``_whisper_model``
    slot. Otherwise a fake model from one test would leak into the
    next."""
    monkeypatch.setattr(doc_extract, "_whisper_model", None, raising=False)
    yield
    monkeypatch.setattr(doc_extract, "_whisper_model", None, raising=False)


def _install_fake_model(
    monkeypatch: pytest.MonkeyPatch,
    segments: list[_FakeSegment],
) -> _FakeWhisperModel:
    fake = _FakeWhisperModel(segments=segments)
    monkeypatch.setattr(doc_extract, "_get_whisper", lambda: fake)
    return fake


# ── happy path ───────────────────────────────────────────────────


def test_extract_audio_concatenates_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(
        monkeypatch,
        [
            _FakeSegment(text="Hello world."),
            _FakeSegment(text="This is the second segment."),
            _FakeSegment(text="Third one."),
        ],
    )
    text = _extract_audio("call.mp3", b"\x00\x01\x02fake-audio")
    assert text == "Hello world. This is the second segment. Third one."


def test_extract_audio_strips_segment_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(
        monkeypatch,
        [
            _FakeSegment(text="  leading   "),
            _FakeSegment(text="   trailing  "),
        ],
    )
    text = _extract_audio("a.wav", b"RIFF....")
    # Each segment is .strip()'d and joined with a single space.
    assert text == "leading trailing"


def test_extract_audio_empty_segments_returns_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch, [])  # corrupted / silent clip
    text = _extract_audio("silence.wav", b"\x00" * 64)
    assert text == ""


def test_extract_audio_multilingual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Uzbek (Latin), Russian (Cyrillic), English — all round-trip.
    _install_fake_model(
        monkeypatch,
        [
            _FakeSegment(text="Salom dunyo, bu test."),
            _FakeSegment(text="Привет мир, это тест."),
            _FakeSegment(text="Hello world, this is a test."),
        ],
    )
    text = _extract_audio("meeting.m4a", b"fake")
    assert "Salom dunyo" in text
    assert "Привет мир" in text
    assert "Hello world" in text


# ── ffmpeg missing → clear RuntimeError ──────────────────────────


def test_extract_audio_wraps_ffmpeg_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenModel:
        def transcribe(self, path: str, language=None):  # noqa: D401
            raise FileNotFoundError(
                "[Errno 2] No such file or directory: 'ffmpeg'"
            )

    monkeypatch.setattr(doc_extract, "_get_whisper", lambda: _BrokenModel())

    with pytest.raises(RuntimeError, match="ffmpeg not found on PATH"):
        _extract_audio("call.mp3", b"fake")


def test_extract_audio_does_not_swallow_other_filenotfound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A FileNotFoundError that's NOT about ffmpeg should bubble up
    untouched so callers can see the real cause."""

    class _BrokenModel:
        def transcribe(self, path: str, language=None):  # noqa: D401
            raise FileNotFoundError("Some other missing file.dat")

    monkeypatch.setattr(doc_extract, "_get_whisper", lambda: _BrokenModel())

    with pytest.raises(FileNotFoundError, match="missing file.dat"):
        _extract_audio("call.mp3", b"fake")


# ── dispatch through extract_text() ──────────────────────────────


@pytest.mark.parametrize(
    "filename,expected_mime",
    [
        ("call.mp3", "audio/mpeg"),
        ("clip.mp4", "video/mp4"),
        ("voice.wav", "audio/wav"),
        ("note.opus", "audio/opus"),
        ("voicemail.m4a", "audio/mp4"),
        ("screencast.webm", "audio/webm"),
        ("podcast.ogg", "audio/ogg"),
    ],
)
def test_extract_text_dispatches_audio(
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    expected_mime: str,
) -> None:
    _install_fake_model(
        monkeypatch,
        [_FakeSegment(text="transcribed audio body")],
    )
    out = extract_text(filename, b"some-bytes")
    assert out is not None
    text, mime = out
    assert text == "transcribed audio body"
    assert mime == expected_mime


def test_extract_text_audio_passes_temp_path_to_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the extractor actually writes a temp file with the
    correct extension and hands its path to ``transcribe()``."""
    fake = _install_fake_model(
        monkeypatch,
        [_FakeSegment(text="ok")],
    )
    extract_text("interview.mp3", b"audio-bytes-here")
    assert len(fake.calls) == 1
    assert fake.calls[0].endswith(".mp3")


def test_extract_text_audio_temp_file_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os as _os

    fake = _install_fake_model(
        monkeypatch,
        [_FakeSegment(text="ok")],
    )
    extract_text("interview.mp3", b"audio-bytes-here")
    # Tempfile should be deleted in the finally block.
    assert not _os.path.exists(fake.calls[0])


# ── allow-list contract ─────────────────────────────────────────


def test_audio_extensions_in_supported_set() -> None:
    for ext in [".mp3", ".mp4", ".m4a", ".wav", ".webm", ".ogg", ".opus"]:
        assert ext in SUPPORTED_EXTENSIONS, ext
        assert ext in _AUDIO_EXTS, ext


# ── transcript cap ──────────────────────────────────────────────


def test_extract_audio_caps_long_transcripts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Produce ~3 MB of segments to force the MAX_TEXT_BYTES (1 MB) cap.
    chunk = "word " * 1000  # ~5 KB
    _install_fake_model(
        monkeypatch,
        [_FakeSegment(text=chunk) for _ in range(800)],
    )
    text = _extract_audio("long.mp3", b"fake")
    encoded_len = len(text.encode("utf-8"))
    # _cap appends a short "[...truncated]" marker after slicing, so the
    # final length is MAX_TEXT_BYTES + that marker's length.
    assert encoded_len <= doc_extract.MAX_TEXT_BYTES + 32
    assert text.endswith("[...truncated]")
