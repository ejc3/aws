import hashlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

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
                b"2024-01-02T04:34:23+01:00 host CRON[15]: " + hostname + b".\n"
                b"2024-01-02T04:00:01+00:00 host CRON[22]: outside\n"
                b"2024-01-02T03:34:23Z host CRON[9]: not." + hostname + b"\n"
            )

            result = compare.compare(pre, post, query)
            self.assertEqual(result["comparison"]["host"]["system_services"]["changed"], 1)
            self.assertEqual(result["comparison"]["host"]["cron"]["new"], 2)
            self.assertEqual(result["comparison"]["host"]["cron"]["changed_mtime_in_window"], 1)
            self.assertEqual(result["comparison"]["host"]["cron"]["post_ioc_exact"], 1)
            self.assertEqual(result["post_coverage"]["special_files_skipped"], 1)
            self.assertEqual(result["post_log_counts"]["host"]["cron"], 2)
            self.assertEqual(result["post_log_counts"]["host"]["ioc_in_log"], 1)
            self.assertNotIn("private", str(result))
            self.assertNotIn(hostname.decode(), str(result))

    def test_walks_guest_persistence_and_logs_without_guest_names(self):
        query = compare.Query({
            "window_start_utc": "2024-01-02T03:00:00Z",
            "window_end_utc": "2024-01-02T04:00:00Z",
        })
        with tempfile.TemporaryDirectory() as root:
            pre, post = Path(root) / "before", Path(root) / "after"
            for image in (pre, post):
                guest = image / "var/lib/machines/private-guest"
                for path in ("etc/systemd/system", "var/log", "home/private-user"):
                    (guest / path).mkdir(parents=True)
                (guest / "home/private-user/.profile").write_text(image.name)
                (guest.parent / "other-private-name").symlink_to(guest, target_is_directory=True)
            guest = post / "var/lib/machines/private-guest"
            (guest / "etc/systemd/system/private-service").write_text("test")
            (guest / "var/log/syslog").write_bytes(b"2024-01-02T03:10:00Z host CRON[42]: synthetic\n")
            result = compare.compare(pre, post, query)
        self.assertEqual(result["post_coverage"]["guest_roots"], 1)
        self.assertEqual(result["comparison"]["guest"]["system_services"]["new"], 1)
        self.assertNotIn("user_startup", result["comparison"]["guest"])
        self.assertEqual(result["post_log_counts"]["guest"]["cron"], 1)
        self.assertNotIn("private", str(result))

    def test_journal_summary_counts_only_relevant_content(self):
        query = compare.Query({
            "window_start_utc": "2024-01-02T03:00:00Z",
            "window_end_utc": "2024-01-02T04:00:00Z",
            "hostname_sha256": hashlib.sha256(b"example.invalid").hexdigest(),
        })
        source = io.BytesIO(b'{"MESSAGE":"example.invalid.","SYSLOG_IDENTIFIER":"CRON"}\n'
                            b'{"MESSAGE":"example.invalid.evil","SYSLOG_IDENTIFIER":"CRON"}\n'
                            b'{"MESSAGE":"Accepted something","SYSLOG_IDENTIFIER":"sshd"}\n'
                            b'{"MESSAGE":[42],"SYSLOG_IDENTIFIER":"unknown"}\n')
        counts = compare.collections.Counter()
        with mock.patch.object(compare, "MAX_JOURNAL_RECORDS", 3):
            compare.summarize_journal(source, query, counts)
        self.assertEqual(dict(counts), {
            "journal_records_checked": 3, "journal_ioc_exact": 1,
            "journal_cron": 2, "journal_ssh_accepted": 1,
        })

    def test_bounded_directory_names(self):
        with tempfile.TemporaryDirectory() as root:
            for number in range(12):
                (Path(root) / str(number)).touch()
            fd = os.open(root, compare.DIR)
            try:
                counts = compare.collections.Counter()
                self.assertEqual(len(compare.entries(fd, 4, counts)), 4)
                self.assertEqual(counts["omitted_directory_entries"], 1)
            finally:
                os.close(fd)

    def test_rejects_unbounded_window(self):
        with self.assertRaises(ValueError):
            compare.Query({"window_start_utc": "2024-01-01T03:00:00Z", "window_end_utc": "2024-01-03T04:00:00Z"})


if __name__ == "__main__":
    unittest.main(buffer=True)
