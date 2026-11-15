"""Guard: ground truth is for evaluation only.

data/ground_truth.json holds the true canonical values. If application code could read it,
the pipeline would be graded on answers it had seen. This test fails if any file under
app/ or core/ mentions the ground-truth file or the ground_truth name, or imports it.
"""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUARDED_DIRS = [ROOT / "app", ROOT / "core"]
FORBIDDEN = re.compile(r"ground_truth", re.IGNORECASE)


def _python_files():
    for d in GUARDED_DIRS:
        if d.exists():
            yield from d.rglob("*.py")


def test_guarded_dirs_exist():
    for d in GUARDED_DIRS:
        assert d.is_dir(), f"expected {d} to exist, otherwise this guard checks nothing"


@pytest.mark.parametrize("path", list(_python_files()), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_ground_truth_reference(path):
    text = path.read_text(encoding="utf-8")
    hits = [i + 1 for i, line in enumerate(text.splitlines()) if FORBIDDEN.search(line)]
    assert not hits, f"{path.relative_to(ROOT)} references ground truth on lines {hits}"


def test_no_ground_truth_import_by_ast():
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert "ground_truth" not in name, f"{path.relative_to(ROOT)} imports {name}"
