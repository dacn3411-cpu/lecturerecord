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


class TestKoreanFilenames(unittest.TestCase):
    """Korean filenames once crashed the Gemini upload with UnicodeEncodeError (2026-11-26)."""

    def test_ascii_safe_name(self):
        fn = _load_function(MAIN_SCRIPT, "ascii_safe_name", {})
        for name in ["2026-11-03_아동진단 4.m4a", "2026-11-26_사례기반 11.m4a", "ADL 1.m4a"]:
            with self.subTest(name=name):
                safe = fn(name)
                safe.encode("ascii")  # must not raise
                self.assertTrue(safe.endswith(".m4a"))
        self.assertEqual(fn("ADL 1.m4a"), "ADL 1.m4a")

    def test_upload_does_not_pass_str_path(self):
        src = MAIN_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("file=str(file_path)", src)


class TestDriveState(unittest.TestCase):
    """A downloaded file must be retried until it is analyzed (audio is not committed to git)."""

    def setUp(self):
        self.root = Path("/repo")
        ns = {"Path": Path, "ROOT_DIR": self.root, "Dict": dict, "Any": object}
        self.is_done = _load_function(MAIN_SCRIPT, "drive_file_is_done", ns)
        self.mark_done = _load_function(MAIN_SCRIPT, "mark_drive_file_done", ns)

    def test_done_flag(self):
        ids = {"legacy": {"name": "a"}, "new": {"name": "b", "done": False}}
        self.assertTrue(self.is_done(ids, "legacy"))  # entries from before the flag
        self.assertFalse(self.is_done(ids, "new"))  # downloaded but not analyzed -> retry
        self.assertFalse(self.is_done(ids, "unknown"))

    def test_mark_done(self):
        state = {"processed_drive_ids": {"x": {"local_path": "recordings/a.m4a", "done": False}}}
        self.mark_done(state, self.root / "recordings" / "a.m4a")
        self.assertTrue(state["processed_drive_ids"]["x"]["done"])


class TestNotionTitleFormatting(unittest.TestCase):
    """Ensure Notion page titles strip duplicate course names, ignore 6-digit YYMMDD dates as week numbers, and format cleanly."""

    def setUp(self):
        from typing import Optional

        ns = {
            "re": re,
            "Path": Path,
            "datetime": datetime,
            "Optional": Optional,
            "OFFICIAL_COURSES": [
                "감각과인지재활",
                "명작의 탄생",
                "삼킴재활",
                "일상생활활동",
                "아동운동재활학",
                "장애아동진단 및 평가",
                "지역사회재활",
                "사례기반 정신사회작업치료학",
            ],
            "COURSE_ALIASES": {
                "adl": "일상생활활동",
                "지역사회": "지역사회재활",
                "삼킴": "삼킴재활",
                "아진": "장애아동진단 및 평가",
            },
        }
        self.extract_num = _load_function(MAIN_SCRIPT, "extract_lecture_number_from_stem", ns)
        self.strip_prefix = _load_function(MAIN_SCRIPT, "strip_redundant_course_and_week_prefix", ns)
        self.build_title = _load_function(MAIN_SCRIPT, "build_clean_lecture_page_title", ns)
        self.format_concise = _load_function(MAIN_SCRIPT, "format_concise_notion_title", ns)

    def test_extract_lecture_number_ignores_yymmdd(self):
        self.assertEqual(self.extract_num("2026-11-24_음성 261124_180737"), "")
        self.assertEqual(self.extract_num("2026-10-28_지역사회재활 1"), "1")
        self.assertEqual(self.extract_num("2026-11-11_지역사회 5"), "5")
        self.assertEqual(self.extract_num("ADL 16"), "16")

    def test_build_clean_lecture_page_title(self):
        self.assertEqual(
            self.build_title(
                "2026-10-28",
                "지역사회재활",
                "recordings/2026-10-28_지역사회재활 1.m4a",
                "지역사회재활 1주차 - 지역사회중심재활(CBR) 운영 예시",
            ),
            "10/28 1주차 지역사회중심재활(CBR) 운영 예시",
        )
        self.assertEqual(
            self.build_title(
                "2026-11-11",
                "지역사회재활",
                "recordings/2026-11-11_지역사회 5.m4a",
                "11주차 - 지역사회 기반 작업치료 중재와 프로그램 운영 전략",
            ),
            "11/11 5주차 지역사회 기반 작업치료 중재와 프로그램 운영 전략",
        )
        self.assertEqual(
            self.build_title(
                "2026-11-24",
                "지역사회재활",
                "recordings/2026-11-24_음성 261124_180737.m4a",
                "지역사회중심재활(CBR)과 사회적 처방(Social Prescribing)의 이해",
            ),
            "11/24 지역사회중심재활(CBR)과 사회적 처방(Social Prescribing)의 이해",
        )

    def test_format_concise_notion_title(self):
        self.assertEqual(
            self.format_concise("10/28 1주차 지역사회재활 1주차 - 지역사회중심재활(CBR)", "지역사회재활"),
            "10/28 1주차 지역사회중심재활(CBR)",
        )
        self.assertEqual(
            self.format_concise("11/24 261124주차 지역사회중심재활(CBR)과 사회적 처방", "지역사회재활"),
            "11/24 지역사회중심재활(CBR)과 사회적 처방",
        )
        self.assertEqual(
            self.format_concise("11/24 핵심 아티클 (261124, 261124주차)", "지역사회재활"),
            "11/24 핵심 아티클",
        )
        self.assertEqual(
            self.format_concise("11/26 핵심 아티클 (10, 9주차)", "지역사회재활"),
            "11/26 핵심 아티클 (9, 10주차)",
        )


if __name__ == "__main__":
    unittest.main()

