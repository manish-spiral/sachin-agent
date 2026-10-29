"""Architecture fitness function: modules cannot import each other."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MODULES = {"scraper", "matcher", "applier"}


def _iter_imports(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                yield node.module


@pytest.mark.parametrize("module", sorted(MODULES))
def test_no_cross_module_imports(module: str):
    module_dir = ROOT / module
    assert module_dir.exists(), f"{module} dir missing"
    violations: list[str] = []
    for py_file in module_dir.rglob("*.py"):
        tree = ast.parse(py_file.read_text(), filename=str(py_file))
        for imp in _iter_imports(tree):
            root = imp.split(".")[0]
            if root in MODULES and root != module:
                violations.append(f"{py_file.relative_to(ROOT)} imports {imp}")
    assert not violations, "Cross-module imports detected:\n" + "\n".join(violations)

def test_orchestrator_only_talks_through_subprocesses():
    """The orchestrator composes the modules by running their CLIs; importing
    them would couple it to their internals."""
    violations = []
    for py_file in (ROOT / "orchestrator").rglob("*.py"):
        tree = ast.parse(py_file.read_text(), filename=str(py_file))
        violations += [f"{py_file.name} imports {imp}" for imp in _iter_imports(tree)
                       if imp.split(".")[0] in MODULES]
    assert not violations, violations


def test_shared_never_imports_modules():
    violations = []
    for py_file in (ROOT / "shared").rglob("*.py"):
        tree = ast.parse(py_file.read_text(), filename=str(py_file))
        violations += [f"{py_file.relative_to(ROOT)} imports {imp}" for imp in _iter_imports(tree)
                       if imp.split(".")[0] in MODULES | {"orchestrator"}]
    assert not violations, violations
