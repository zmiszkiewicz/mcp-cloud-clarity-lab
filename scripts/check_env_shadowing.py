#!/usr/bin/env python3
"""
Catch a Python assignment that looks like configuration and silently is not.

    LAB_REQUIRE_TEST_VM=0
    REQUIRE_TEST_VM = os.environ.get("LAB_REQUIRE_TEST_VM", "1") not in ("0",)

The first line binds a Python variable. It does NOT set an environment
variable, so the `get` on the second line still returns its default and the
edit does nothing at all. No error, no warning, no effect — the worst
combination, because it looks like it worked. This cost a track restart.

WHAT IT MUST NOT FLAG. The normal idiom reads and assigns the same name:

    BROKER_API_URL = os.environ.get("BROKER_API_URL", "https://...")

That is correct and is everywhere in this repo. A regex cannot tell the two
apart, so this walks the AST instead and flags only an assignment whose value
is a plain literal — a name that is read from the environment and also pinned
to a constant is the mistake; a name assigned *from* the environment is not.

Run from preflight.sh.
"""

import ast
import pathlib
import sys


def env_keys_read(tree):
    """Every literal key this module passes to os.environ / os.environ.get."""
    keys = set()

    for node in ast.walk(tree):
        # os.environ.get("NAME", ...) and os.getenv("NAME", ...)
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None)
            if name in ("get", "getenv") and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    keys.add(first.value)
        # os.environ["NAME"]
        elif isinstance(node, ast.Subscript):
            value = node.value
            if getattr(value, "attr", None) == "environ":
                index = node.slice
                if isinstance(index, ast.Constant) and isinstance(index.value, str):
                    keys.add(index.value)

    return keys


def literal_assignments(tree):
    """Module-level `NAME = <literal>` bindings, as {name: lineno}."""
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Constant):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.isupper():
                found[target.id] = node.lineno
    return found


def check(directory):
    problems = []
    here = pathlib.Path(__file__).name

    for path in sorted(pathlib.Path(directory).glob("*.py")):
        if path.name == here:
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue                       # py_compile already reports these

        read = env_keys_read(tree)
        for name, lineno in literal_assignments(tree).items():
            if name in read:
                problems.append(
                    f"{path.name}:{lineno}: {name} is pinned to a literal AND "
                    f"read from os.environ in this file. The assignment does "
                    f"nothing — change the default passed to os.environ.get(), "
                    f"or set os.environ[{name!r}] explicitly."
                )
    return problems


def main():
    problems = check(pathlib.Path(__file__).parent)
    if problems:
        for problem in problems:
            print(f"      {problem}")
        return 1
    print("      no environment variable is shadowed by a literal assignment")
    return 0


if __name__ == "__main__":
    sys.exit(main())
