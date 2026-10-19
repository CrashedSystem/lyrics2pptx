"""Progress-reporting tests (``crawl.progress``), fully offline.

The point of this module is that it is *safe*: reporting must never change a
collect, never raise into it, and never leak across threads. Those three
properties are asserted here, plus the arithmetic that turns ``done``/``total``
into the single percentage the UI is allowed to show.
"""

from __future__ import annotations

import threading
import unittest

from lyrics2pptx.crawl import progress


class ProgressUpdateTests(unittest.TestCase):
    def test_percent_exists_only_when_the_total_is_known(self) -> None:
        self.assertIsNone(progress.ProgressUpdate("가사 페이지 확인 중…").percent)
        self.assertIsNone(progress.ProgressUpdate("가사 페이지 확인 중…", done=1).percent)
        self.assertIsNone(progress.ProgressUpdate("가사 페이지 확인 중…", done=1, total=0).percent)

    def test_percent_is_done_over_total(self) -> None:
        self.assertEqual(progress.ProgressUpdate("x", done=1, total=4).percent, 25)
        self.assertEqual(progress.ProgressUpdate("x", done=3, total=4).percent, 75)
        self.assertEqual(progress.ProgressUpdate("x", done=4, total=4).percent, 100)

    def test_percent_is_clamped(self) -> None:
        self.assertEqual(progress.ProgressUpdate("x", done=9, total=4).percent, 100)
        self.assertEqual(progress.ProgressUpdate("x", done=-2, total=4).percent, 0)

    def test_has_fraction_matches_percent(self) -> None:
        self.assertTrue(progress.ProgressUpdate("x", done=0, total=3).has_fraction)
        self.assertFalse(progress.ProgressUpdate("x", done=0, total=0).has_fraction)


class ProgressSinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(progress.set_sink, None)

    def test_reporting_without_a_sink_is_a_no_op(self) -> None:
        self.assertIsNone(progress.current_sink())
        progress.report("아무도 듣지 않는다")  # must not raise
        progress.report("아무도 듣지 않는다", done=1, total=2)

    def test_reports_reach_the_installed_sink(self) -> None:
        seen: list = []
        progress.set_sink(seen.append)
        progress.report("가사 페이지 확인 중…", done=2, total=5)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].message, "가사 페이지 확인 중…")
        self.assertEqual(seen[0].percent, 40)

    def test_a_broken_sink_never_breaks_the_collect(self) -> None:
        def boom(_update) -> None:
            raise RuntimeError("sink is broken")

        progress.set_sink(boom)
        progress.report("그래도 수집은 계속된다")  # must not raise

    def test_clearing_the_sink_stops_reports(self) -> None:
        seen: list = []
        progress.set_sink(seen.append)
        progress.set_sink(None)
        progress.report("더 이상 듣지 않는다")
        self.assertEqual(seen, [])

    def test_the_sink_is_per_thread(self) -> None:
        main_seen: list = []
        worker_seen: list = []
        progress.set_sink(main_seen.append)
        started = threading.Event()

        def worker() -> None:
            try:
                # No sink yet in this thread: the main thread's must not be used.
                progress.report("작업 스레드: 싱크 없음")
                progress.set_sink(worker_seen.append)
                progress.report("작업 스레드: 싱크 있음")
            finally:
                progress.set_sink(None)
                started.set()

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(started.wait(5.0), "worker thread did not finish")
        thread.join(timeout=5.0)

        progress.report("메인 스레드")
        self.assertEqual([u.message for u in main_seen], ["메인 스레드"])
        self.assertEqual([u.message for u in worker_seen], ["작업 스레드: 싱크 있음"])


if __name__ == "__main__":
    unittest.main()
