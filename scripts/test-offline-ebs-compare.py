import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("compare", Path(__file__).with_name("offline-ebs-compare.py"))
compare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compare)


class OfflineCompare(unittest.TestCase):
    def test_counts_changes_without_following_links_or_exporting_contents(self):
        with tempfile.TemporaryDirectory() as root:
            pre, post = Path(root) / "before", Path(root) / "after"
            for point in pre, post:
                for directory in ("etc/systemd/system", "etc/cron.d", "var/log"):
                    (point / directory).mkdir(parents=True, exist_ok=True)

            hostname = b"example.invalid"
            query = compare.Query({
                "window_start_utc": "2024-01-02T03:00:00Z",
                "window_end_utc": "2024-01-02T04:00:00Z",
                "hostname_sha256": hashlib.sha256(hostname).hexdigest(),
            })
            (pre / "etc/systemd/system/private-service").write_text("before")
            (post / "etc/systemd/system/private-service").write_text("after")
            cron = post / "etc/cron.d/private-task"
            cron.write_bytes(b"https://" + hostname.upper() + b"/synthetic-private-payload")
            mtime = query.start.timestamp() + 1
            os.utime(cron, (mtime, mtime))
            (post / "etc/cron.d/private-link").symlink_to(cron)
            os.mkfifo(post / "etc/cron.d/private-fifo")
            (post / "var/log/syslog").write_bytes(
                b"2024-01-02T04:34:23+01:00 host CRON[15]: " + hostname + b"\n"
                b"2024-01-02T04:00:01+00:00 host CRON[22]: outside\n"
                b"2024-01-02T03:34:23Z host CRON[9]: not." + hostname + b"\n"
            )

            result = compare.compare(pre, post, query)
            self.assertEqual(result["comparison"]["system_services"]["changed"], 1)
            self.assertEqual(result["comparison"]["cron"]["new"], 2)
            self.assertEqual(result["comparison"]["cron"]["changed_mtime_in_window"], 1)
            self.assertEqual(result["comparison"]["cron"]["post_ioc_exact"], 1)
            self.assertEqual(result["post_coverage"]["special_files_skipped"], 1)
            self.assertEqual(result["post_log_counts"]["cron"], 2)
            self.assertEqual(result["post_log_counts"]["ioc_in_log"], 1)
            self.assertNotIn("private", str(result))
            self.assertNotIn(hostname.decode(), str(result))

    def test_rejects_unbounded_window(self):
        with self.assertRaises(ValueError):
            compare.Query({"window_start_utc": "2024-01-01T03:00:00Z", "window_end_utc": "2024-01-03T04:00:00Z"})


if __name__ == "__main__":
    unittest.main(buffer=True)
