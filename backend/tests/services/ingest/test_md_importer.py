import io
import zipfile

from app.services.ingest.base import IngestItem
from app.services.ingest.md_importer import extract_wikilinks, parse_zip

# [plan-fix] plan set `pytestmark = pytest.mark.asyncio`, but every test here is
# synchronous (pure parsing, no DB) — the mark only produced PytestWarnings.


def make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_parse_zip_returns_ingest_items():
    zip_bytes = make_zip(
        {
            "notes/hello.md": "# Hello World\n\nThis is a note.",
            "notes/bye.md": "# Goodbye\n\nSee [[Hello World]] for details.",
        }
    )
    items, edge_specs = parse_zip(zip_bytes, source="md_upload")
    assert len(items) == 2
    assert all(isinstance(i, IngestItem) for i in items)
    titles = [i.title for i in items]
    assert "Hello World" in titles
    assert "Goodbye" in titles


def test_parse_zip_extracts_wikilink_edge_specs():
    zip_bytes = make_zip(
        {
            "a.md": "# A\n\nLinks to [[B]].",
            "b.md": "# B\n\nContent.",
        }
    )
    items, edge_specs = parse_zip(zip_bytes, source="md_upload")
    assert any(e.source_ref.endswith("a.md") and e.target_ref.endswith("b.md") for e in edge_specs)


def test_extract_wikilinks():
    links = extract_wikilinks("See [[Alpha]] and [[Beta]].")
    assert links == ["Alpha", "Beta"]


def test_non_md_files_skipped():
    zip_bytes = make_zip({"image.png": b"\x89PNG", "doc.md": "# Doc\n\nContent."})
    items, _ = parse_zip(zip_bytes, source="md_upload")
    assert len(items) == 1
    assert items[0].title == "Doc"


def _zip_of(files: dict[str, str]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return buf.getvalue()


def test_empty_h1_falls_back_to_filename():
    """An H1 of pure whitespace must not produce an empty title."""
    items, _ = parse_zip(_zip_of({"meeting-notes.md": "#   \n\nbody text"}))
    assert items[0].title == "Meeting Notes"


def test_duplicate_titles_resolve_first_wins_deterministically():
    """Two files with the same H1: wikilinks resolve to the FIRST (zip order),
    deterministically — never silently to whichever was processed last."""
    files = {
        "a-first.md": "# Meeting Notes\n\nfirst",
        "b-second.md": "# Meeting Notes\n\nsecond",
        "linker.md": "# Linker\n\nsee [[Meeting Notes]]",
    }
    items, edges = parse_zip(_zip_of(files))
    links = [e for e in edges if e.source_ref == "linker.md"]
    assert len(links) == 1
    assert links[0].target_ref == "a-first.md"
