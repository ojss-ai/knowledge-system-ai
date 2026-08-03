from app.services.chunking import chunk_markdown


def test_empty_body():
    chunks = chunk_markdown("", max_tokens=512)
    assert chunks == []


def test_short_body_is_single_chunk():
    body = "This is a short note."
    chunks = chunk_markdown(body, max_tokens=512)
    assert len(chunks) == 1
    assert chunks[0] == body


def test_heading_split():
    body = "# Section A\n\nContent A.\n\n# Section B\n\nContent B."
    chunks = chunk_markdown(body, max_tokens=512)
    assert len(chunks) == 2
    assert "Content A" in chunks[0]
    assert "Content B" in chunks[1]


def test_long_section_splits_by_tokens():
    # 600 words, each ~1.3 tokens ≈ 780 tokens — should split with max_tokens=512
    body = "word " * 600
    chunks = chunk_markdown(body, max_tokens=512)
    assert len(chunks) >= 2


def test_chunk_overlap():
    body = "word " * 600
    chunks = chunk_markdown(body, max_tokens=512, overlap_tokens=64)
    # With overlap, second chunk should share some words with first
    first_words = set(chunks[0].split()[-30:])
    second_words = set(chunks[1].split()[:30])
    assert len(first_words & second_words) > 0
