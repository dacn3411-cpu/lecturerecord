"""Quick checks that catch bugs which only show up at runtime.

Uses only the standard library (no pip install needed), so it runs in a few seconds:
    python -m unittest discover -s tests -v
"""
import ast
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = sorted((Path(__file__).resolve().parent.parent / "scripts").glob("*.py"))
MAIN_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "process_recordings.py"
RE_FUNCS = {"compile", "search", "match", "fullmatch", "findall", "finditer", "sub", "subn", "split"}


def _regex_literals(tree):
    """Yield (lineno, pattern) for every re.<func>("literal", ...) call."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
            and node.func.attr in RE_FUNCS
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            yield node.lineno, node.args[0].value


def _load_function(path, name, namespace):
    """Extract a (possibly nested) function from a script and exec it in isolation."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
            return namespace[name]
    raise AssertionError(f"{name} not found in {path.name}")


class TestScripts(unittest.TestCase):
    def test_scripts_compile(self):
        for path in SCRIPTS:
            with self.subTest(script=path.name):
                compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_all_regex_literals_compile(self):
        for path in SCRIPTS:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for lineno, pattern in _regex_literals(tree):
                with self.subTest(script=path.name, line=lineno, pattern=pattern):
                    re.compile(pattern)


class TestParseDriveModDate(unittest.TestCase):
    KST = timezone(timedelta(hours=9))

    def setUp(self):
        ns = {"re": re, "datetime": datetime, "KST": self.KST}
        self.parse = _load_function(MAIN_SCRIPT, "parse_drive_mod_date", ns)
        self.year = datetime.now(self.KST).year
        self.today = datetime.now(self.KST).strftime("%Y-%m-%d")

    def _html(self, raw):
        return f'<div class="flip-entry-last-modified"><div>{raw}</div>'

    def test_formats(self):
        cases = {
            "Nov 16": f"{self.year}-11-16",
            "November 16": f"{self.year}-11-16",
            "2026. 11. 16.": "2026-11-16",
            "2026-11-16": "2026-11-16",
            "11/16/26": "2026-11-16",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(self.parse(self._html(raw)), expected)

    def test_unknown_falls_back_to_today(self):
        for raw in ["3:45 PM", "garbage"]:
            with self.subTest(raw=raw):
                self.assertEqual(self.parse(self._html(raw)), self.today)
        self.assertEqual(self.parse("<div>no date block</div>"), self.today)


if __name__ == "__main__":
    unittest.main()
