from datetime import date

import pytest

from shopcare.schemas.policy_models import PolicyDocument
from shopcare.services.policy_chunker import split_policy


def make_policy(content="ABCDEFGHI", **overrides):
    values = dict(
        policy_id="demo",
        version="v1",
        title="模拟政策",
        category="数码",
        effective_from=date(2026, 10, 1),
        effective_until=None,
        content=content,
    )
    values.update(overrides)
    return PolicyDocument(**values)


@pytest.mark.parametrize(
    "text,size,overlap,spans",
    [
        ("ABC", 4, 1, [(0, 3)]),
        ("ABCD", 4, 1, [(0, 4)]),
        ("ABCDEFGHI", 4, 1, [(0, 4), (3, 7), (6, 9)]),
        ("ABCDEFGH", 4, 0, [(0, 4), (4, 8)]),
        ("ABCDEF", 4, 3, [(0, 4), (1, 5), (2, 6)]),
        ("ABCDEFGHIJ", 4, 2, [(0, 4), (2, 6), (4, 8), (6, 10)]),
    ],
)
def test_chunk_windows_and_tail(text, size, overlap, spans):
    chunks = split_policy(make_policy(text), chunk_size=size, overlap=overlap)
    assert [(c.start_char, c.end_char) for c in chunks] == spans
    assert [c.text for c in chunks] == [text[start:end] for start, end in spans]
    assert [c.chunk_id for c in chunks] == [f"demo:v1:{i:03d}" for i in range(len(spans))]


@pytest.mark.parametrize("size,overlap", [(0, 0), (-1, 0), (4, -1), (4, 4), (4, 5)])
def test_invalid_window_parameters(size, overlap):
    with pytest.raises(ValueError):
        split_policy(make_policy(), chunk_size=size, overlap=overlap)


def test_unicode_and_whitespace_offsets_preserve_original_text():
    text = "  耳机🎧故障。\n换货需确认。  "
    chunks = split_policy(make_policy(text), chunk_size=7, overlap=2)
    assert chunks[0].start_char == 0
    assert chunks[-1].end_char == len(text)
    assert all(c.text == text[c.start_char : c.end_char] for c in chunks)
    assert chunks[0].text.startswith("  ")
    assert chunks[-1].text.endswith("  ")


def test_chunk_copies_policy_metadata():
    policy = make_policy(effective_until=date(2026, 11, 1))
    for chunk in split_policy(policy, chunk_size=4, overlap=1):
        for field in (
            "policy_id",
            "version",
            "title",
            "category",
            "effective_from",
            "effective_until",
        ):
            assert getattr(chunk, field) == getattr(policy, field)


def test_chunking_does_not_modify_source_policy():
    policy = make_policy()
    before = policy.model_dump()
    split_policy(policy, chunk_size=4, overlap=1)
    assert policy.model_dump() == before


def test_chunking_is_repeatable():
    policy = make_policy()
    assert split_policy(policy, 4, 1) == split_policy(policy, 4, 1)


@pytest.mark.parametrize("size,overlap", [(1, 0), (2, 0), (2, 1), (3, 2), (300, 50)])
def test_chunks_reconstruct_full_source(size, overlap):
    text = "这是一段模拟规则。\n" * 17
    chunks = split_policy(make_policy(text), chunk_size=size, overlap=overlap)
    assert all(0 < len(c.text) <= size for c in chunks)
    assert chunks[0].start_char == 0
    assert chunks[-1].end_char == len(text)
    for previous, current in zip(chunks, chunks[1:]):
        assert current.start_char == previous.end_char - overlap
    reconstructed = chunks[0].text + "".join(c.text[overlap:] for c in chunks[1:])
    assert reconstructed == text
