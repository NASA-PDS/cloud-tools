"""Tests for the shared S3 helpers."""
import unittest
from unittest import mock

from botocore.exceptions import ClientError

from pds.pdc.migration import s3_common
from pds.pdc.migration.errors import MigrationStoppedError


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "test"}}, "DeleteObjects")


class ResumableThrottleTest(unittest.TestCase):
    def test_retries_slowdown_then_succeeds(self):
        calls = []

        def action():
            calls.append(1)
            if len(calls) < 3:
                raise client_error("SlowDown")
            return "ok"

        with mock.patch.object(s3_common.time, "sleep") as sleep:
            self.assertEqual(s3_common.resumable(action), "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 10])

    def test_stops_when_throttling_persists(self):
        def action():
            raise client_error("SlowDown")

        with mock.patch.object(s3_common.time, "sleep"):
            with self.assertRaises(MigrationStoppedError):
                s3_common.resumable(action)

    def test_other_errors_stop_immediately(self):
        calls = []

        def action():
            calls.append(1)
            raise client_error("AccessDenied")

        with mock.patch.object(s3_common.time, "sleep") as sleep:
            with self.assertRaises(MigrationStoppedError):
                s3_common.resumable(action)
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
