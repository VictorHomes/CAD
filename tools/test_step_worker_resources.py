"""Windows launcher regression: measure and stop the real interpreter tree."""
from pathlib import Path
import tempfile
import unittest
import psutil
from build123d import Box,export_step
from evaluate_step_conversion import run_one


class WorkerResourceTests(unittest.TestCase):
    def run_fixture(self,limit,timeout):
        parent=Path(__file__).resolve().parents[1]/'output'/'step_test_fixtures'
        parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            root=Path(directory)
            source=root/'box.step'
            export_step(Box(10,20,30),source)
            result=run_one((source,'brep_exact',root/'box.py',root/'box.json'),timeout,limit)
            # No still-running descendant may retain this job's unique path.
            for child in psutil.Process().children(recursive=True):
                try:
                    self.assertFalse(any(directory in arg for arg in child.cmdline()))
                except psutil.Error:
                    pass
            return result

    def test_peak_includes_real_interpreter(self):
        record=self.run_fixture(2048,45)
        self.assertTrue(record['technical_success'])
        self.assertGreater(record['peak_rss_mb'],100)
        self.assertGreater(record['worker_peak_rss_mb'],100)

    def test_memory_limit_and_tree_cleanup(self):
        record=self.run_fixture(128,45)
        self.assertFalse(record['technical_success'])
        self.assertEqual(record['error_code'],'MEMORY_LIMIT')

    def test_timeout_and_tree_cleanup(self):
        record=self.run_fixture(2048,.4)
        self.assertFalse(record['technical_success'])
        self.assertEqual(record['error_code'],'TIMEOUT')


if __name__=='__main__':
    unittest.main()
