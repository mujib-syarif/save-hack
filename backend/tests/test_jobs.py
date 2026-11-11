import threading
import time
import unittest
from unittest.mock import patch

from savehack.config import settings
from savehack.engines.direct import DirectEngine
from savehack.errors import AppError, ErrorCode
from savehack.jobs import JobManager
from savehack.ratelimit import RateLimiter
from savehack.router import Router

from tests.helpers import PAYLOAD_SHA, NetCase
import hashlib


def wait_for(pred, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class JobTests(NetCase):
    def setUp(self):
        super().setUp()
        self.router = Router(direct=DirectEngine())
        self.mgr = JobManager(self.router, base_dir=self.workdir / "dl")
        self.addCleanup(self.mgr.shutdown)

    def analyze(self, path, owner="1.1.1.1"):
        info = DirectEngine().analyze(self.server.url(path))
        return self.mgr.create(info, owner)

    def test_manager_can_be_restarted_after_shutdown(self):
        self.mgr.shutdown()
        self.mgr.start_background()
        job = self.analyze("/file.bin")
        self.mgr.start(job.id, "file", "1.1.1.1")
        self.assertTrue(job.finished.wait(8))
        self.assertEqual(job.status, "done")

    def test_download_completes_and_serves_correct_file(self):
        job = self.analyze("/file.bin")
        self.mgr.start(job.id, "file", "1.1.1.1")
        self.assertTrue(job.finished.wait(8))
        self.assertEqual(job.status, "done")
        self.assertEqual(hashlib.sha256(job.result_path.read_bytes()).hexdigest(), PAYLOAD_SHA)
        pub = job.to_public()
        self.assertEqual(pub["file"]["size"], 300000)
        self.assertEqual(pub["progress"]["percent"], 100.0)

    def test_unknown_format_and_unknown_job(self):
        job = self.analyze("/file.bin")
        with self.assertRaises(AppError) as cm:
            self.mgr.start(job.id, "not-offered", "1.1.1.1")
        self.assertEqual(cm.exception.code, ErrorCode.FORMAT_UNAVAILABLE)
        with self.assertRaises(AppError) as cm:
            self.mgr.get("deadbeef" * 4)
        self.assertEqual(cm.exception.code, ErrorCode.MEDIA_NOT_FOUND)

    def test_cancel_stops_download_and_removes_files(self):
        job = self.analyze("/slow.bin")
        self.mgr.start(job.id, "file", "1.1.1.1")
        self.assertTrue(wait_for(lambda: job.ctx.progress.downloaded > 0))
        self.mgr.cancel(job.id)
        self.assertTrue(job.finished.wait(5))
        self.assertEqual(job.status, "cancelled")
        self.assertFalse(job.workdir.exists())

    def test_delete_cancels_running_job_and_forgets_it(self):
        job = self.analyze("/slow.bin")
        self.mgr.start(job.id, "file", "1.1.1.1")
        self.assertTrue(wait_for(lambda: job.ctx.progress.downloaded > 0))
        self.mgr.delete(job.id)
        self.assertTrue(job.finished.wait(5))
        self.assertFalse(job.workdir.exists())
        with self.assertRaises(AppError):
            self.mgr.get(job.id)

    def test_failed_job_reports_safe_error_and_can_be_retried(self):
        job = self.analyze("/file.bin")
        job.info.formats[0].payload["direct_url"] = self.server.url("/error404")
        self.mgr.start(job.id, "file", "1.1.1.1")
        self.assertTrue(job.finished.wait(8))
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.to_public()["error"]["code"], "MEDIA_NOT_FOUND")
        self.assertNotIn("127.0.0.1", str(job.to_public()))  # no internal detail leaks
        job.info.formats[0].payload["direct_url"] = self.server.url("/file.bin")
        self.mgr.start(job.id, "file", "1.1.1.1")  # retry the same job
        self.assertTrue(job.finished.wait(8))
        self.assertEqual(job.status, "done")
        self.assertEqual(job.runs, 2)

    def test_cannot_start_a_running_job_twice(self):
        job = self.analyze("/slow.bin")
        self.mgr.start(job.id, "file", "1.1.1.1")
        with self.assertRaises(AppError) as cm:
            self.mgr.start(job.id, "file", "1.1.1.1")
        self.assertEqual(cm.exception.code, ErrorCode.RATE_LIMITED)
        self.mgr.delete(job.id)

    def test_per_client_active_job_limit(self):
        with patch.object(settings, "max_active_jobs_per_ip", 2), patch.object(settings, "max_concurrent_jobs", 4):
            jobs = [self.analyze("/slow.bin", owner="9.9.9.9") for _ in range(3)]
            self.mgr.start(jobs[0].id, "file", "9.9.9.9")
            self.mgr.start(jobs[1].id, "file", "9.9.9.9")
            with self.assertRaises(AppError) as cm:
                self.mgr.start(jobs[2].id, "file", "9.9.9.9")
            self.assertEqual(cm.exception.code, ErrorCode.RATE_LIMITED)
            other = self.analyze("/slow.bin", owner="8.8.8.8")  # another client is unaffected
            self.mgr.start(other.id, "file", "8.8.8.8")
            for j in jobs + [other]:
                self.mgr.delete(j.id)

    def test_global_queue_limit(self):
        with patch.object(settings, "max_concurrent_jobs", 1), patch.object(settings, "max_queued_jobs", 1), patch.object(settings, "max_active_jobs_per_ip", 10):
            js = [self.analyze("/slow.bin", owner=f"10.0.0.{i}") for i in range(3)]
            self.mgr.start(js[0].id, "file", "10.0.0.0")
            self.mgr.start(js[1].id, "file", "10.0.0.1")
            with self.assertRaises(AppError) as cm:
                self.mgr.start(js[2].id, "file", "10.0.0.2")
            self.assertEqual(cm.exception.code, ErrorCode.RATE_LIMITED)
            for j in js:
                self.mgr.delete(j.id)

    def test_declared_size_over_limit_rejected_at_start(self):
        job = self.analyze("/big.bin")
        with patch.object(settings, "max_download_mb", 1):
            with self.assertRaises(AppError) as cm:
                self.mgr.start(job.id, "file", "1.1.1.1")
        self.assertEqual(cm.exception.code, ErrorCode.STORAGE_LIMIT)

    def test_storage_quota_rejected_at_start(self):
        job = self.analyze("/file.bin")
        with patch.object(settings, "max_storage_mb", 1), patch.object(self.mgr, "storage_used", return_value=1024 * 1024):
            with self.assertRaises(AppError) as cm:
                self.mgr.start(job.id, "file", "1.1.1.1")
        self.assertEqual(cm.exception.code, ErrorCode.STORAGE_LIMIT)

    def test_sweep_removes_expired_files_but_not_active_jobs(self):
        done = self.analyze("/file.bin")
        self.mgr.start(done.id, "file", "1.1.1.1")
        self.assertTrue(done.finished.wait(8))
        active = self.analyze("/slow.bin", owner="2.2.2.2")
        self.mgr.start(active.id, "file", "2.2.2.2")
        self.assertTrue(done.workdir.exists())
        self.assertEqual(self.mgr.sweep(now=time.time()), 0)  # fresh
        self.assertEqual(self.mgr.sweep(now=time.time() + settings.job_ttl * 10), 1)
        self.assertFalse(done.workdir.exists())
        self.assertTrue(active.workdir.exists())
        self.assertEqual(self.mgr.get(active.id).id, active.id)
        self.mgr.delete(active.id)

    def test_startup_purges_leftover_job_dirs_from_previous_process(self):
        stale = self.mgr.base / ("a" * 32)
        stale.mkdir()
        (stale / "half.part").write_bytes(b"x" * 10)
        keep = self.mgr.base / "not-a-job-dir"
        keep.mkdir()
        self.mgr.purge_leftovers()
        self.assertFalse(stale.exists())
        self.assertTrue(keep.exists())

    def test_job_deadline_stops_long_download(self):
        with patch.object(settings, "job_timeout", 30):
            job = self.analyze("/slow.bin")
            self.mgr.start(job.id, "file", "1.1.1.1")
            job.ctx.deadline = time.monotonic() + 0.2
            self.assertTrue(job.finished.wait(5))
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error.code, ErrorCode.EXTRACTION_TIMEOUT)

    def test_storage_used_counts_job_files(self):
        job = self.analyze("/file.bin")
        self.mgr.start(job.id, "file", "1.1.1.1")
        self.assertTrue(job.finished.wait(8))
        self.assertGreaterEqual(self.mgr.storage_used(), 300000)
        self.mgr.delete(job.id)
        self.assertEqual(self.mgr.storage_used(), 0)


class RateLimiterTests(unittest.TestCase):
    def test_blocks_after_limit_and_recovers(self):
        rl = RateLimiter(3, window=60)
        self.assertEqual([rl.allow("a", now=t) for t in (0, 1, 2, 3)], [True, True, True, False])
        self.assertTrue(rl.allow("a", now=61))  # oldest hit left the window

    def test_clients_are_independent(self):
        rl = RateLimiter(1, window=60)
        self.assertTrue(rl.allow("a", now=0))
        self.assertFalse(rl.allow("a", now=1))
        self.assertTrue(rl.allow("b", now=1))

    def test_retry_after(self):
        rl = RateLimiter(1, window=60)
        rl.allow("a", now=0)
        self.assertEqual(rl.retry_after("a", now=10), 51)
        self.assertEqual(rl.retry_after("nobody"), 0)

    def test_memory_is_bounded(self):
        rl = RateLimiter(5, window=60, max_keys=100)
        for i in range(1000):
            rl.allow(f"ip{i}", now=i * 0.001)
        self.assertLessEqual(len(rl._hits), 100)

    def test_thread_safety(self):
        rl = RateLimiter(50, window=60)
        results = []
        ts = [threading.Thread(target=lambda: results.append(rl.allow("x"))) for _ in range(200)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sum(results), 50)


if __name__ == "__main__":
    unittest.main()
