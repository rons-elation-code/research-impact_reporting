"""Static-analysis test: original_source_url never flows into forbidden sinks (AC15.1, AC25.1)."""
from __future__ import annotations

import ast
import pathlib


FORBIDDEN_SINK_NAMES = {
    "get", "head", "request",
    "urlopen", "fetch",
    "classify", "prompt", "complete", "create",
    "invoke", "ainvoke",
    "ask", "generate",
}
FORBIDDEN_PROVENANCE_REFS = {"original_source_url", "original_source_url_redacted"}


def _references_provenance(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in FORBIDDEN_PROVENANCE_REFS:
            return True
        if isinstance(child, ast.Attribute) and child.attr in FORBIDDEN_PROVENANCE_REFS:
            return True
    return False


def _build_taint_set(tree: ast.AST) -> set[str]:
    tainted: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _references_provenance(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    tainted.add(target.id)
                elif isinstance(target, ast.Tuple):
                    for elt in target.elts:
                        if isinstance(elt, ast.Name):
                            tainted.add(elt.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None and _references_provenance(node.value):
                tainted.add(node.target.id)
    return tainted


def _references_taint(node: ast.AST, tainted: set[str]) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in tainted:
            return True
    return False


def _src_files() -> list[pathlib.Path]:
    repo_root = pathlib.Path(__file__).resolve().parents[4]
    return [
        p for p in (repo_root / "lavandula" / "reports").rglob("*.py")
        if "/tests/" not in str(p) and "__pycache__" not in str(p)
    ]


def test_original_source_url_never_passed_to_forbidden_sink():
    violations = []
    for f in _src_files():
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError:
            continue
        tainted = _build_taint_set(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func_name = (
                node.func.attr if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", None)
            )
            if func_name not in FORBIDDEN_SINK_NAMES:
                continue
            for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                if _references_provenance(arg) or _references_taint(arg, tainted):
                    violations.append(f"{f}:{node.lineno}: {func_name}()")
    assert not violations, (
        "original_source_url flowed into a forbidden sink:\n"
        + "\n".join(violations)
    )


def test_original_source_url_redacted_is_written_by_db_writer():
    repo_root = pathlib.Path(__file__).resolve().parents[4]
    src = (repo_root / "lavandula" / "reports" / "db_writer.py").read_text()
    assert "original_source_url_redacted" in src
