import tempfile
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from repo_walker import ScanConfig
from scanner import CodebaseScanner


def make_temp_repo(files: dict[str, str]) -> str:
    d = tempfile.mkdtemp()
    for name, content in files.items():
        p = Path(d) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return d


CALLS_CODE = textwrap.dedent("""
    def alpha():
        beta()

    def beta():
        pass
""")


def test_collect_produces_file_and_symbol_items() -> None:
    repo = make_temp_repo({"lib.py": CALLS_CODE})
    items, _ = CodebaseScanner(
        ScanConfig(repo_path=repo, languages=["python"], dry_run=True)
    ).collect()
    titles = [i.title for i in items]
    assert "lib.py" in titles and "alpha" in titles and "beta" in titles
    assert {i.node_type for i in items} == {"code_file", "code_symbol"}
    repo_tag = f"codebase:{Path(repo).name}"
    assert all(repo_tag in i.tags for i in items)


def test_defines_edge_is_file_to_symbol() -> None:
    repo = make_temp_repo({"lib.py": CALLS_CODE})
    _, edges = CodebaseScanner(
        ScanConfig(repo_path=repo, languages=["python"], dry_run=True)
    ).collect()
    defines = [e for e in edges if e.label == "DEFINES"]
    assert defines and all(e.source_ref == "lib.py" and "#" in e.target_ref for e in defines)


def test_calls_edge_carries_confidence() -> None:
    repo = make_temp_repo({"lib.py": CALLS_CODE})
    _, edges = CodebaseScanner(
        ScanConfig(repo_path=repo, languages=["python"], dry_run=True)
    ).collect()
    calls = [e for e in edges if e.label == "CALLS"]
    assert any(
        e.source_ref.endswith("#lib.alpha")
        and e.target_ref.endswith("#lib.beta")
        and e.confidence == 0.7
        for e in calls
    )


def test_dry_run_makes_no_api_calls() -> None:
    repo = make_temp_repo({"mod.py": "def foo(): pass"})
    scanner = CodebaseScanner(ScanConfig(repo_path=repo, languages=["python"], dry_run=True))
    with patch("requests.Session.post") as mock_post:
        result = scanner.run()
        mock_post.assert_not_called()
    assert result.total >= 1


def test_incremental_second_scan_is_empty() -> None:
    repo = make_temp_repo({"mod.py": "def foo(): pass"})
    config = ScanConfig(repo_path=repo, languages=["python"], dry_run=True)
    CodebaseScanner(config).run()
    assert CodebaseScanner(config).run().new_items == 0


def test_run_posts_batches_to_ingest_batch() -> None:
    repo = make_temp_repo({"lib.py": CALLS_CODE})
    config = ScanConfig(
        repo_path=repo, languages=["python"], kb_token="tok", kb_api_url="http://kb.local"
    )
    scanner = CodebaseScanner(config)
    ok = MagicMock(status_code=200)
    ok.json.return_value = {
        "created": 3,
        "updated": 0,
        "skipped": 0,
        "edges_queued": 3,
        "edges_dangling": 0,
    }
    with patch("requests.Session.post", return_value=ok) as mock_post:
        result = scanner.run()
    urls = [c.args[0] for c in mock_post.call_args_list]
    assert urls and all(u == "http://kb.local/api/v1/uploads/ingest-batch" for u in urls)
    first_payload: dict[str, Any] = mock_post.call_args_list[0].kwargs["json"]
    assert set(first_payload) == {"items", "edges"}
    assert result.new_items == 3 and result.failed_batches == 0


def test_changed_caller_still_links_to_unchanged_callee() -> None:
    """Symbol table spans ALL files; items only re-emit for changed ones."""
    repo = make_temp_repo({"a.py": "def alpha():\n    beta()\n", "b.py": "def beta(): pass"})
    config = ScanConfig(repo_path=repo, languages=["python"], dry_run=True)
    CodebaseScanner(config).run()  # everything cached
    Path(repo, "a.py").write_text("def alpha():\n    beta()\n    beta()\n")
    items, edges = CodebaseScanner(config).collect()
    assert all(i.source_ref.startswith("a.py") for i in items)  # only a.py re-emits
    assert any(e.label == "CALLS" and e.target_ref.endswith("#b.beta") for e in edges)
