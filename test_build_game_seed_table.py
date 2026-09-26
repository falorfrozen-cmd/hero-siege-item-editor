"""build_game_seed_table.py: one run builds the whole table.

The game keeps none of the items an evaluation request builds (measured
2026-09-26, ForgePact docs/item-truth-memory-research.md), so the tool has no
per-session limit by default. --max-per-run still stops a run early, and the work
file lets the next run carry on where it left off.
"""
import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("build_game_seed_table.py")
SPEC = importlib.util.spec_from_file_location("build_game_seed_table_tests", MODULE_PATH)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


class RunLimitTests(unittest.TestCase):
    def test_no_limit_stays_no_limit(self):
        self.assertIsNone(tool.spend(None, 250_000))

    def test_a_limit_is_spent_phase_by_phase(self):
        self.assertEqual(6, tool.spend(10, 4))
        self.assertEqual(0, tool.spend(6, 6))

    def test_a_spent_limit_asks_the_game_for_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "itemtruth"
            work = tool.Work(Path(tmp) / "work.json")
            work.plan("measure", [(3, {"w": 1.0, "a": 1.0, "b": 0.0, "c": 0.0}, ["white"])])
            self.assertEqual(0, tool.measure(work, root, work.missing("measure"), 0))
            self.assertFalse((root / "requests").exists(), "no request was written")

    def test_the_default_is_no_limit(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn('parser.add_argument("--max-per-run", type=int, default=0,', source)
        self.assertIn("budget = args.max_per_run if args.max_per_run > 0 else None", source)
        self.assertNotIn("budget -= measure(", source, "every phase goes through spend()")

    def test_nothing_asks_for_a_game_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = tool.Work(Path(tmp) / "work.json")
            work.plan("measure", [(3, {"w": 1.0, "a": 1.0}, ["white"])])
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(2, tool.pause(work))
        self.assertIn("1 items still to measure", out.getvalue())
        self.assertNotIn("restart", out.getvalue().lower())
        self.assertIn("One run builds the whole table", tool.__doc__)
        self.assertNotIn("keeps every item", tool.__doc__)


if __name__ == "__main__":
    unittest.main()
