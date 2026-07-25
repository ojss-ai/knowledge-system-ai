# tools/kb-codebase-scan/scanner.py
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import requests

from language_parser import LanguageParser, ParsedFile
from python_parser import PythonParser
from repo_walker import RepoWalker, ScanConfig
from typescript_parser import TypeScriptParser

logger = logging.getLogger("kb-codebase-scan")

_CALL_CONFIDENCE = 0.7   # heuristic static resolution (ADR-009)
_MAX_CALL_TARGETS = 3    # cap fan-out per ambiguous call name
_BATCH_ITEMS = 200       # server cap (IngestBatchIn)
_BATCH_EDGES = 2000
_TIMEOUT_S = 30


@dataclass
class ScanIngestItem:
    source: str
    source_ref: str
    title: str
    body: str
    node_type: str = "code_symbol"
    visibility: str = "private"
    tags: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScanEdgeSpec:
    source_ref: str
    target_ref: str
    label: str = "CALLS"
    confidence: float | None = None


@dataclass
class ScanResult:
    total: int = 0
    new_items: int = 0
    updated_items: int = 0
    skipped_files: int = 0
    failed_files: int = 0
    failed_batches: int = 0
    edges_sent: int = 0
    edges_dangling: int = 0
    api_calls: int = 0


class CodebaseScanner:
    """Walk → parse → POST /api/v1/uploads/ingest-batch (items + ref edges).

    All symbols across the repo feed the fqn table; only CHANGED files emit
    items/edges. Edges to unchanged targets resolve server-side (DB fallback);
    first-run refs resolve in-batch. DEFINES is file → symbol (canonical)."""

    def __init__(self, config: ScanConfig) -> None:
        self._config = config
        self._root = Path(config.repo_path).resolve()
        self._repo_tag = f"codebase:{self._root.name}"
        self._walker = RepoWalker(config)
        parsers: list[LanguageParser] = [PythonParser(), TypeScriptParser()]
        self._parsers: dict[str, LanguageParser] = {
            ext: p for p in parsers for ext in p.extensions
        }
        self._kb_session = requests.Session()
        self._kb_session.headers["Authorization"] = f"Bearer {config.kb_token}"
        self._kb_session.headers["Content-Type"] = "application/json"
        self._changed: list[Path] = []

    def _ref(self, rel_path: str, fqn: str | None = None) -> str:
        base = f"{self._config.source_ref_prefix}{rel_path}"
        return f"{base}#{fqn}" if fqn else base

    def _parse(self, path: Path) -> ParsedFile | None:
        parser = self._parsers.get(path.suffix)
        if parser is None:
            return None
        rel = str(path.relative_to(self._root)).replace("\\", "/")
        try:
            return parser.parse(rel, path.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:  # error-tolerant by design (ADR-009)
            logger.warning(f"Parse error in {path}: {exc}")
            return None

    def collect(self) -> tuple[list[ScanIngestItem], list[ScanEdgeSpec]]:
        """Parse the repo once; emit items + edges for changed files only."""
        self._changed = list(self._walker.iter_changed_files())
        changed_rel = {str(p.relative_to(self._root)).replace("\\", "/") for p in self._changed}
        parsed_files = [pf for p in self._walker.iter_source_files() if (pf := self._parse(p))]

        fqn_to_ref: dict[str, str] = {}
        for pf in parsed_files:
            for sym in pf.symbols:
                fqn_to_ref[sym.fqn] = self._ref(pf.file_path, sym.fqn)

        items: list[ScanIngestItem] = []
        edges: list[ScanEdgeSpec] = []
        for pf in parsed_files:
            if pf.file_path not in changed_rel:
                continue
            file_ref = self._ref(pf.file_path)
            imports_md = "\n".join(f"- `{i}`" for i in pf.imports[:20])
            items.append(
                ScanIngestItem(
                    source="codebase",
                    source_ref=file_ref,
                    title=Path(pf.file_path).name,
                    body=(
                        f"# {Path(pf.file_path).name}\n\nModule: `{pf.module_fqn}`\n\n"
                        f"**Imports:**\n{imports_md}"
                    ),
                    node_type="code_file",
                    visibility=self._config.visibility,
                    tags=["code", pf.language, self._repo_tag],
                    meta={
                        "language": pf.language,
                        "module_fqn": pf.module_fqn,
                        "file_path": pf.file_path,
                    },
                )
            )
            for sym in pf.symbols:
                sym_ref = self._ref(pf.file_path, sym.fqn)
                items.append(
                    ScanIngestItem(
                        source="codebase",
                        source_ref=sym_ref,
                        title=sym.name,
                        body=(
                            f"# `{sym.fqn}`\n\nType: {sym.kind.value}  "
                            f"Lines: {sym.line_start}-{sym.line_end}\n\n{sym.docstring}"
                        ).strip(),
                        node_type="code_symbol",
                        visibility=self._config.visibility,
                        tags=["code", pf.language, sym.kind.value, self._repo_tag],
                        meta={
                            "fqn": sym.fqn,
                            "kind": sym.kind.value,
                            "language": pf.language,
                            "file_path": pf.file_path,
                            "line_start": sym.line_start,
                        },
                    )
                )
                edges.append(
                    ScanEdgeSpec(source_ref=file_ref, target_ref=sym_ref, label="DEFINES")
                )
                for called in sym.calls:
                    matches = [f for f in fqn_to_ref if f.endswith(f".{called}")]
                    for match in matches[:_MAX_CALL_TARGETS]:
                        if fqn_to_ref[match] != sym_ref:
                            edges.append(
                                ScanEdgeSpec(
                                    source_ref=sym_ref,
                                    target_ref=fqn_to_ref[match],
                                    label="CALLS",
                                    confidence=_CALL_CONFIDENCE,
                                )
                            )
        return items, edges

    def _post_batch(
        self,
        items: list[ScanIngestItem],
        edges: list[ScanEdgeSpec],
        result: ScanResult,
    ) -> None:
        r = self._kb_session.post(
            f"{self._config.kb_api_url}/api/v1/uploads/ingest-batch",
            json={"items": [asdict(i) for i in items], "edges": [asdict(e) for e in edges]},
            timeout=_TIMEOUT_S,
        )
        result.api_calls += 1
        r.raise_for_status()
        data = r.json()
        if items:  # item counters only from item batches (edge batches create nothing)
            result.new_items += int(data["created"])
            result.updated_items += int(data["updated"])
        if edges:
            result.edges_sent += int(data["edges_queued"])
            result.edges_dangling += int(data["edges_dangling"])

    def run(self) -> ScanResult:
        result = ScanResult()
        items, edges = self.collect()
        result.total = len(items)

        if self._config.dry_run:
            logger.info(f"[DRY RUN] Would upsert {len(items)} items, {len(edges)} edges")
            result.new_items = len(items)
        else:
            failed = False
            # Items first (all batches), edges after: every ref is committed
            # before any edge resolution — dangling only for genuinely absent refs.
            for start in range(0, len(items), _BATCH_ITEMS):
                try:
                    self._post_batch(items[start : start + _BATCH_ITEMS], [], result)
                except Exception as exc:
                    logger.error(f"Batch upsert failed: {exc}")
                    result.failed_batches += 1
                    failed = True
            for start in range(0, len(edges), _BATCH_EDGES):
                try:
                    self._post_batch([], edges[start : start + _BATCH_EDGES], result)
                except Exception as exc:
                    logger.error(f"Edge batch failed: {exc}")
                    result.failed_batches += 1
                    failed = True
            if failed:
                result.failed_files = result.failed_batches  # Task 5 exit-code signal
                return result  # cache NOT saved → next run re-sends (idempotent upserts)

        for path in self._changed:
            self._walker.mark_scanned(path)
        self._walker.save_cache()
        return result
