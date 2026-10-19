#!/usr/bin/env python3
"""Oracle: every public function/class in the given directories must have a docstring.

Prints {"deficits": [{"kind": "missing_docstring", "target": "path::qualname", "detail": ...}], "scope": N}
where scope = number of public definitions seen (so deleting code cannot pass as a fix).
"""
import ast
import json
import sys
from pathlib import Path


def walk(root: Path):
    for py in sorted(root.rglob("*.py")):
        tree = ast.parse(py.read_text(), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not node.name.startswith("_"):
                yield py, node


def main(dirs: list[str]) -> None:
    deficits, scope = [], 0
    for d in dirs:
        for py, node in walk(Path(d)):
            scope += 1
            if ast.get_docstring(node) is None:
                deficits.append({"kind": "missing_docstring", "target": f"{py.as_posix()}::{node.name}",
                                 "detail": f"line {node.lineno}"})
    print(json.dumps({"deficits": deficits, "scope": scope}))


if __name__ == "__main__":
    main(sys.argv[1:] or ["src"])
