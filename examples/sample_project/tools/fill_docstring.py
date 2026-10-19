#!/usr/bin/env python3
"""Deterministic demo supplier: insert a docstring for the one definition named in $DL_DEFICIT.

No model, no network. It exists to show the loop mechanics; swap it for an
agent CLI via DEFICIT_LOOP_AGENT_CMD when you want real supply.
"""
import ast
import json
import os
import sys
from pathlib import Path


def main() -> int:
    deficit = json.loads(os.environ["DL_DEFICIT"])
    if deficit["kind"] != "missing_docstring":
        print(f"cannot handle kind {deficit['kind']}", file=sys.stderr)
        return 1
    path_s, name = deficit["target"].split("::", 1)
    path = Path(path_s)
    src = path.read_text()
    tree = ast.parse(src)
    node = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name)
    lines = src.splitlines(keepends=True)
    body_line = node.body[0].lineno - 1          # first statement; docstring goes right before it
    indent = lines[body_line][: len(lines[body_line]) - len(lines[body_line].lstrip())]
    words = name.replace("_", " ").strip()
    kind = "Class" if isinstance(node, ast.ClassDef) else "Function"
    lines.insert(body_line, f'{indent}"""{kind} {words}."""\n')
    path.write_text("".join(lines))
    print(f"added docstring to {deficit['target']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
