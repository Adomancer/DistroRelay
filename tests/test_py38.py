"""Kode yang ikut ke Linux kit harus jalan di Python 3.8 (distro fresh). Tidak ada 3.8 di laptop, jadi dicek statis."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).parent.parent / "src"
KIT_FILES = sorted(
    [SRC / "distrorelay" / "__init__.py"]
    + list((SRC / "distrorelay" / "common").glob("*.py"))
    + list((SRC / "distrorelay_linux").glob("*.py"))
)
GENERIC_BUILTINS = {"list", "dict", "tuple", "set", "frozenset", "type"}
NEW_METHODS = {"removeprefix", "removesuffix"}  # 3.9+
NEW_MODULES = {"tomllib", "zoneinfo", "graphlib"}  # 3.9+/3.11+ (tomllib boleh di dalam fungsi khusus Windows)


class Checker(ast.NodeVisitor):
    def __init__(self):
        self.problems = []

    def _annotation(self, node):
        """Anotasi tidak dievaluasi (from __future__ import annotations), jadi `list[str]` di sana aman."""

    def visit_FunctionDef(self, node):
        for arg in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
            arg.annotation = None
        for extra in (node.args.vararg, node.args.kwarg):
            if extra is not None:
                extra.annotation = None
        node.returns = None
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_AnnAssign(self, node):
        if node.value is not None:
            self.visit(node.value)

    def visit_Subscript(self, node):
        if isinstance(node.value, ast.Name) and node.value.id in GENERIC_BUILTINS:
            self.problems.append(f"baris {node.lineno}: {node.value.id}[...] dievaluasi saat runtime (3.9+)")
        self.generic_visit(node)

    def visit_BinOp(self, node):
        names = [n for n in (node.left, node.right) if isinstance(n, ast.Name)]
        if isinstance(node.op, ast.BitOr) and any(n.id in GENERIC_BUILTINS | {"str", "int", "None"} for n in names):
            self.problems.append(f"baris {node.lineno}: tipe X | Y dievaluasi saat runtime (3.10+)")
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if node.attr in NEW_METHODS:
            self.problems.append(f"baris {node.lineno}: .{node.attr}() baru ada di 3.9")
        self.generic_visit(node)


@pytest.mark.parametrize("path", KIT_FILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_kit_code_runs_on_python38(path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path), feature_version=(3, 8))  # sintaks 3.9+ (match, dll.) gagal di sini
    if any(isinstance(n, (ast.AnnAssign, ast.FunctionDef)) for n in ast.walk(tree)):
        assert "from __future__ import annotations" in source, "anotasi harus tidak dievaluasi di 3.8"
    for node in tree.body:  # modul baru di level atas (di dalam fungsi boleh, mis. tomllib khusus Windows)
        names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
            [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        assert not set(names) & NEW_MODULES, f"{path.name}: import {names} tidak ada di 3.8"
    checker = Checker()
    checker.visit(tree)
    assert checker.problems == []


def test_checker_catches_runtime_only_features():
    source = (
        "from __future__ import annotations\n"
        "def f(x: list[str]) -> dict[str, int]:\n"  # anotasi: aman
        "    y: list[int] = []\n"
        "    Alias = list[str]\n"  # runtime: tidak aman
        "    isinstance(x, int | str)\n"
        "    return 'a'.removeprefix('a')\n"
    )
    checker = Checker()
    checker.visit(ast.parse(source))
    assert len(checker.problems) == 3, checker.problems


def test_kit_package_does_not_import_windows_code():
    for path in KIT_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            names = [a.name for a in node.names] if isinstance(node, (ast.Import, ast.ImportFrom)) else []
            joined = " ".join([module or ""] + names)
            assert "windows" not in joined and "textual" not in joined and "rich" not in joined, f"{path}: {joined}"
