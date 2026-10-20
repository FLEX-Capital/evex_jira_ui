"""Historical aggregates and archived notebook sources remain frozen and honestly labelled."""

import hashlib
import json
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from pptx import Presentation

import build_board_deck

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests/fixtures/board-snapshot.json"
PROVENANCE = ROOT / "analysis/historical/board-provenance.json"


def source_digest(path: Path) -> str:
    """Hash notebook cell identities and source independently of saved execution state."""
    notebook = json.loads(path.read_text())
    cells = [{k: cell.get(k) for k in ("cell_type", "id", "source")} for cell in notebook["cells"]]
    return hashlib.sha256(json.dumps(cells, sort_keys=True).encode()).hexdigest()


def slide_snapshot(path: Path) -> dict[str, Any]:
    """Hash normalized full slide XML, including text, numbers, formatting and geometry."""
    deck = Presentation(str(path))
    hashes = []
    for slide in deck.slides:
        root = ET.fromstring(slide._element.xml)
        for element in root.iter():
            for key in list(element.attrib):
                if key.rsplit("}", 1)[-1] in {"id", "creationId"}:
                    del element.attrib[key]
        hashes.append(hashlib.sha256(ET.tostring(root)).hexdigest())
    return {"width": deck.slide_width, "height": deck.slide_height, "slides": hashes}


def test_board_snapshot_is_unchanged_by_archive(tmp_path: Path) -> None:
    """All ten slides retain their captured text/numerical/geometry snapshot."""
    output = tmp_path / "historical.pptx"
    build_board_deck.build(str(output))
    actual = slide_snapshot(output)
    assert actual == json.loads(BASELINE.read_text())["deck"]
    assert len(actual["slides"]) == 10
    assert abs(actual["width"] / actual["height"] - 16 / 9) < 0.001


def test_historical_provenance_marks_unknown_fields() -> None:
    """Current source evidence never fabricates historical extraction or classifier identity."""
    manifest = json.loads(PROVENANCE.read_text())
    assert manifest["version"] == 1 and manifest["project"] == "SDAX"
    assert manifest["provenance_status"] == "legacy_unverified"
    for name in (
        "original_extraction_timestamp",
        "original_cohort_end",
        "original_classifier_fingerprint",
    ):
        assert manifest[name] is None
    for source in manifest["sources"]:
        assert hashlib.sha256((ROOT / source["path"]).read_bytes()).hexdigest() == source["sha256"]
    assert manifest["literal_snapshot"] is True


def test_archive_retains_sources_and_clean_execution_state() -> None:
    """Archived cells/IDs match the pre-task capture, and every saved output is absent."""
    for name, digest in json.loads(BASELINE.read_text())["notebooks"].items():
        path = ROOT / "analysis/archive" / name
        assert not (ROOT / name).exists()
        assert source_digest(path) == digest
        notebook = json.loads(path.read_text())
        for cell in notebook["cells"]:
            if cell["cell_type"] == "code":
                assert cell["outputs"] == [] and cell["execution_count"] is None
    index = (ROOT / "analysis/archive/README.md").read_text()
    assert "unsupported" in index.lower() and "dotenv" in index


def test_supported_links_follow_moves() -> None:
    """The supported guide names a single maintained notebook and links the archive."""
    guide = (ROOT / "docs/analysis.md").read_text()
    assert "../amparex_ai_assist_analysis.ipynb" in guide
    assert "../analysis/archive/README.md" in guide
    assert "--group analysis" in guide and "RUN_CLASSIFICATION=False" in guide
    assert "jira_analysis.ipynb" not in guide
    assert "analysis/archive/README.md" in (ROOT / "README.md").read_text()
