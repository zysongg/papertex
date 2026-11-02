import importlib.util
import inspect
import queue
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException


MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "operation_progress.py"


def operation_id(number):
    return f"{number:032x}"


class OperationProgressTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("operation_progress_under_test", MODULE_PATH)
        self.progress = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.progress)
        clock = patch.object(self.progress.time, "monotonic", return_value=1000.0)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.first = operation_id(1)
        self.second = operation_id(2)

    def assert_http(self, status, function, *args, **kwargs):
        with self.assertRaises(HTTPException) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.status_code, status)
        return caught.exception

    def complete(self, identifier):
        return self.progress.tracked_operation(lambda **kwargs: None)(operation_id=identifier)

    def spawn(self, function):
        outcome = queue.Queue()

        def worker():
            try:
                outcome.put((True, function()))
            except BaseException as exc:
                outcome.put((False, exc))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return thread, outcome

    def join_worker(self, worker):
        thread, outcome = worker
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive(), "worker did not finish")
        success, result = outcome.get_nowait()
        if not success:
            raise result
        return result

    def test_running_then_completed_preserves_result(self):
        result = {"data": object()}

        @self.progress.tracked_operation
        def work(operation_id=None):
            self.assertEqual(self.progress.get_progress(operation_id), {
                "id": self.first,
                "status": "running",
                "message": "等待其他写操作完成",
                "elapsed_seconds": 0,
                "stage_elapsed_seconds": 0,
                "details": {},
            })
            self.clock.return_value = 1008
            return result

        self.assertIs(work(operation_id=self.first), result)
        state = self.progress.get_progress(self.first)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["message"], "操作完成")
        self.assertEqual(state["elapsed_seconds"], 8)
        self.assertNotIn("data", state)
        self.assertFalse(hasattr(self.progress._context, "operation_id"))

    def test_warnings_message_and_result_identity(self):
        for number, warnings in enumerate((["check sync"], [], None), start=1):
            with self.subTest(warnings=warnings):
                result = {"warnings": warnings}
                work = self.progress.tracked_operation(lambda **kwargs: result)
                identifier = operation_id(number)
                self.assertIs(work(operation_id=identifier), result)
                state = self.progress.get_progress(identifier)
                self.assertEqual(state["status"], "completed")
                self.assertEqual(state["message"],
                                 "操作完成，但有步骤需要核对" if warnings else "操作完成")
                self.assertNotIn("warnings", state)

    def test_non_dict_result_is_unchanged(self):
        for number, result in enumerate((None, "response", ["value"], object()), start=1):
            with self.subTest(result=result):
                work = self.progress.tracked_operation(lambda **kwargs: result)
                self.assertIs(work(operation_id=operation_id(number)), result)

    def test_wraps_preserves_metadata_and_signature(self):
        def work(value, operation_id=None):
            """Original endpoint documentation."""
            return value

        wrapped = self.progress.tracked_operation(work)
        self.assertEqual(wrapped.__name__, work.__name__)
        self.assertEqual(wrapped.__doc__, work.__doc__)
        self.assertIs(wrapped.__wrapped__, work)
        self.assertEqual(inspect.signature(wrapped), inspect.signature(work))

    def test_none_or_missing_id_is_direct_passthrough(self):
        function = Mock(return_value=object())
        wrapped = self.progress.tracked_operation(function)
        self.assertIs(wrapped(1, operation_id=None), function.return_value)
        function.assert_called_once_with(1, operation_id=None)
        self.assertIs(wrapped(2), function.return_value)
        function.assert_called_with(2)
        self.assertEqual(self.progress._records, {})
        self.clock.assert_not_called()
        self.assertFalse(hasattr(self.progress._context, "operation_id"))

    def test_untracked_exception_is_unchanged(self):
        error = ValueError("original")
        function = Mock(side_effect=error)
        with self.assertRaises(ValueError) as caught:
            self.progress.tracked_operation(function)(operation_id=None)
        self.assertIs(caught.exception, error)
        self.assertEqual(self.progress._records, {})
        self.clock.assert_not_called()

    def test_update_without_context_is_noop(self):
        self.progress.update_progress("ignored", host="host", payload=object())
        self.assertEqual(self.progress._records, {})
        self.clock.assert_not_called()

    def test_stage_clock_changes_only_on_new_message(self):
        @self.progress.tracked_operation
        def work(operation_id=None):
            self.clock.return_value = 1002
            self.progress.update_progress("上传", received_bytes=0, total_bytes=100)
            self.clock.return_value = 1007
            self.progress.update_progress("上传", received_bytes=50)
            state = self.progress.get_progress(operation_id)
            self.assertEqual(state["elapsed_seconds"], 7)
            self.assertEqual(state["stage_elapsed_seconds"], 5)
            self.assertEqual(state["details"], {"received_bytes": 50, "total_bytes": 100})
            self.clock.return_value = 1010
            self.progress.update_progress("同步", host="server")
            self.clock.return_value = 1013
            self.assertEqual(self.progress.get_progress(operation_id)["stage_elapsed_seconds"], 3)

        work(operation_id=self.first)

    def test_details_are_whitelisted_and_copied(self):
        @self.progress.tracked_operation
        def work(operation_id=None):
            details = {
                "host": ["server"], "received_bytes": 12, "total_bytes": 50,
                "attempt": 2, "timeout_seconds": 60,
            }
            self.progress.update_progress("上传", **details, payload="secret", result="secret")
            state = self.progress.get_progress(operation_id)
            self.assertEqual(state["details"], details)
            details["host"].append("external mutation")
            state["details"]["host"].append("poll mutation")
            state["details"]["received_bytes"] = 999
            state["status"] = "failed"
            fresh = self.progress.get_progress(operation_id)
            self.assertEqual(fresh["details"]["host"], ["server"])
            self.assertEqual(fresh["details"]["received_bytes"], 12)
            self.assertEqual(fresh["status"], "running")

        work(operation_id=self.first)

    def test_http_exception_is_recorded_truncated_and_reraised(self):
        for number, exception_type in enumerate((HTTPException, StarletteHTTPException), start=1):
            with self.subTest(exception_type=exception_type):
                error = exception_type(status_code=502, detail="错" * 2100, headers={"X-Test": "yes"})
                wrapped = self.progress.tracked_operation(Mock(side_effect=error))
                identifier = operation_id(number)
                with self.assertRaises(exception_type) as caught:
                    wrapped(operation_id=identifier)
                self.assertIs(caught.exception, error)
                state = self.progress.get_progress(identifier)
                self.assertEqual(state["status"], "failed")
                self.assertEqual(state["http_status"], 502)
                self.assertEqual(state["message"], "错" * 2000)
                self.assertFalse(hasattr(self.progress._context, "operation_id"))

    def test_http_detail_is_converted_to_string(self):
        error = HTTPException(status_code=422, detail={"error": "invalid"})
        wrapped = self.progress.tracked_operation(Mock(side_effect=error))
        self.assert_http(422, wrapped, operation_id=self.first)
        self.assertEqual(self.progress.get_progress(self.first)["message"], str(error.detail))

    def test_unexpected_exception_hides_details_and_cleans_context(self):
        error = RuntimeError("private payload and credentials")
        wrapped = self.progress.tracked_operation(Mock(side_effect=error))
        with self.assertRaises(RuntimeError) as caught:
            wrapped(operation_id=self.first)
        self.assertIs(caught.exception, error)
        state = self.progress.get_progress(self.first)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["message"], "操作失败，请查看服务日志")
        self.assertNotIn("http_status", state)
        self.assertNotIn("private", repr(state))
        self.assertFalse(hasattr(self.progress._context, "operation_id"))
        self.progress.update_progress("must not overwrite")
        self.assertEqual(self.progress.get_progress(self.first), state)

    def test_base_exception_cleans_context_and_releases_active_slot(self):
        for number, error in enumerate((KeyboardInterrupt(), SystemExit(), BaseException()), start=1):
            with self.subTest(error=error):
                wrapped = self.progress.tracked_operation(Mock(side_effect=error))
                identifier = operation_id(number)
                with self.assertRaises(type(error)) as caught:
                    wrapped(operation_id=identifier)
                self.assertIs(caught.exception, error)
                self.assertEqual(self.progress.get_progress(identifier)["status"], "failed")
                self.assertFalse(hasattr(self.progress._context, "operation_id"))
        self.assertTrue(all(record["finished_at"] is not None
                            for record in self.progress._records.values()))

    def test_nested_operations_restore_parent_context(self):
        @self.progress.tracked_operation
        def inner(operation_id=None):
            self.progress.update_progress("inner")
            self.assertEqual(self.progress.get_progress(self.first)["message"], "outer")

        @self.progress.tracked_operation
        def outer(operation_id=None):
            self.progress.update_progress("outer")
            inner(operation_id=self.second)
            self.progress.update_progress("outer resumed")
            self.assertEqual(self.progress.get_progress(self.first)["message"], "outer resumed")
            self.assertEqual(self.progress.get_progress(self.second)["status"], "completed")

        outer(operation_id=self.first)
        self.assertFalse(hasattr(self.progress._context, "operation_id"))

    def test_nested_failures_restore_parent_context(self):
        for number, error in enumerate((HTTPException(503, "offline"), ValueError(), KeyboardInterrupt()), start=1):
            with self.subTest(error=error):
                parent_id = operation_id(number * 2)
                child_id = operation_id(number * 2 + 1)
                inner = self.progress.tracked_operation(Mock(side_effect=error))

                @self.progress.tracked_operation
                def outer(operation_id=None):
                    with self.assertRaises(type(error)):
                        inner(operation_id=child_id)
                    self.progress.update_progress("parent resumed")
                    self.assertEqual(self.progress.get_progress(parent_id)["message"], "parent resumed")

                outer(operation_id=parent_id)
                self.assertFalse(hasattr(self.progress._context, "operation_id"))

    def test_nested_none_inherits_parent_context(self):
        @self.progress.tracked_operation
        def inner(operation_id=None):
            self.progress.update_progress("nested untracked")

        @self.progress.tracked_operation
        def outer(operation_id=None):
            inner(operation_id=None)
            self.assertEqual(self.progress.get_progress(self.first)["message"], "nested untracked")

        outer(operation_id=self.first)
        self.assertEqual(len(self.progress._records), 1)

    def test_invalid_ids_are_rejected_without_execution(self):
        invalid = ("", "a" * 31, "a" * 33, "A" * 32, "g" * 32, "０" * 32,
                   "a" * 31 + "\n", "a" * 32 + "\n", " " + "a" * 32,
                   1, True, b"a" * 32, [], {}, "12345678-1234-1234-1234-123456789012")
        function = Mock()
        wrapped = self.progress.tracked_operation(function)
        for identifier in invalid:
            with self.subTest(identifier=identifier):
                self.assert_http(400, wrapped, operation_id=identifier)
                self.assert_http(400, self.progress.get_progress, identifier)
        self.assert_http(400, self.progress.get_progress, None)
        function.assert_not_called()
        self.assertEqual(self.progress._records, {})

    def test_valid_hex_and_unknown_id(self):
        identifier = "0123456789abcdef0123456789abcdef"
        self.assert_http(404, self.progress.get_progress, identifier)
        self.complete(identifier)
        self.assertEqual(self.progress.get_progress(identifier)["id"], identifier)

    def test_duplicate_completed_and_failed_ids_do_not_execute(self):
        self.complete(self.first)
        failed = self.progress.tracked_operation(Mock(side_effect=HTTPException(500, "failed")))
        self.assert_http(500, failed, operation_id=self.second)
        function = Mock()
        wrapped = self.progress.tracked_operation(function)
        for identifier in (self.first, self.second):
            before = self.progress.get_progress(identifier)
            self.assert_http(409, wrapped, operation_id=identifier)
            self.assertEqual(self.progress.get_progress(identifier), before)
        function.assert_not_called()

    def test_duplicate_running_id_preserves_context(self):
        function = Mock()
        duplicate = self.progress.tracked_operation(function)

        @self.progress.tracked_operation
        def work(operation_id=None):
            self.assert_http(409, duplicate, operation_id=operation_id)
            self.progress.update_progress("still running")
            self.assertEqual(self.progress.get_progress(operation_id)["message"], "still running")

        work(operation_id=self.first)
        function.assert_not_called()

    def test_completed_elapsed_times_are_frozen(self):
        @self.progress.tracked_operation
        def work(operation_id=None):
            self.clock.return_value = 1010

        work(operation_id=self.first)
        before = self.progress.get_progress(self.first)
        self.clock.return_value = 1100
        self.assertEqual(self.progress.get_progress(self.first), before)

    def test_finished_ttl_boundary_and_id_reuse(self):
        self.complete(self.first)
        self.clock.return_value = 1899.999
        self.assertEqual(self.progress.get_progress(self.first)["status"], "completed")
        self.clock.return_value = 1900
        self.assert_http(404, self.progress.get_progress, self.first)
        self.complete(self.first)
        self.assertEqual(self.progress.get_progress(self.first)["status"], "completed")

    def test_failed_records_expire_and_start_prunes(self):
        wrapped = self.progress.tracked_operation(Mock(side_effect=ValueError()))
        with self.assertRaises(ValueError):
            wrapped(operation_id=self.first)
        self.clock.return_value = 1900
        self.complete(self.second)
        self.assertNotIn(self.first, self.progress._records)
        self.assertEqual(len(self.progress._records), 1)

    def test_active_records_never_expire(self):
        @self.progress.tracked_operation
        def work(operation_id=None):
            self.clock.return_value = 10000
            state = self.progress.get_progress(operation_id)
            self.assertEqual(state["status"], "running")
            self.assertEqual(state["elapsed_seconds"], 9000)

        work(operation_id=self.first)
        self.clock.return_value = 10899
        self.assertEqual(self.progress.get_progress(self.first)["status"], "completed")
        self.clock.return_value = 10900
        self.assert_http(404, self.progress.get_progress, self.first)

    def test_capacity_evicts_earliest_finished_not_earliest_started(self):
        @self.progress.tracked_operation
        def outer(operation_id=None):
            self.complete(self.second)
            self.clock.return_value = 1001

        outer(operation_id=self.first)
        for number in range(3, 129):
            self.complete(operation_id(number))
        self.assertEqual(len(self.progress._records), 128)
        self.complete(operation_id(129))
        self.assertEqual(len(self.progress._records), 128)
        self.assert_http(404, self.progress.get_progress, self.second)
        self.assertEqual(self.progress.get_progress(self.first)["status"], "completed")

    def test_capacity_never_evicts_active_record(self):
        identifiers = [operation_id(number) for number in range(2, 131)]

        @self.progress.tracked_operation
        def work(operation_id=None):
            for identifier in identifiers:
                self.complete(identifier)
            self.assertEqual(len(self.progress._records), 128)
            self.assertEqual(self.progress.get_progress(self.first)["status"], "running")
            self.assert_http(404, self.progress.get_progress, self.second)

        work(operation_id=self.first)

    def test_thread_isolation(self):
        ready = threading.Barrier(3)
        release = threading.Event()

        @self.progress.tracked_operation
        def work(label, operation_id=None):
            self.progress.update_progress(label, host=label)
            ready.wait(timeout=3)
            if not release.wait(timeout=3):
                raise AssertionError("worker was not released")
            self.assertEqual(self.progress.get_progress(operation_id)["message"], label)

        workers = [self.spawn(lambda: work("one", operation_id=self.first)),
                   self.spawn(lambda: work("two", operation_id=self.second))]
        try:
            ready.wait(timeout=3)
            self.progress.update_progress("unbound main thread")
            for identifier, label in ((self.first, "one"), (self.second, "two")):
                state = self.progress.get_progress(identifier)
                self.assertEqual(state["message"], label)
                self.assertEqual(state["details"]["host"], label)
        finally:
            release.set()
            for worker in workers:
                self.join_worker(worker)

    def test_poll_is_not_blocked_by_long_task_or_serialized_writer(self):
        write_lock = threading.Lock()
        entered = threading.Event()
        release = threading.Event()
        waiting = threading.Event()

        @self.progress.tracked_operation
        def work(operation_id=None):
            if operation_id == self.second:
                waiting.set()
            with write_lock:
                if operation_id == self.first:
                    entered.set()
                    if not release.wait(timeout=3):
                        raise AssertionError("long task was not released")

        first = self.spawn(lambda: work(operation_id=self.first))
        workers = [first]
        try:
            self.assertTrue(entered.wait(timeout=3))
            workers.append(self.spawn(lambda: work(operation_id=self.second)))
            self.assertTrue(waiting.wait(timeout=3))
            poller = self.spawn(lambda: (self.progress.get_progress(self.first),
                                         self.progress.get_progress(self.second)))
            states = self.join_worker(poller)
            self.assertTrue(all(state["status"] == "running" for state in states))
            self.assertEqual(states[1]["message"], "等待其他写操作完成")
            self.assertFalse(release.is_set())
        finally:
            release.set()
            for worker in workers:
                self.join_worker(worker)

    def test_concurrent_same_id_starts_exactly_once(self):
        barrier = threading.Barrier(3)
        release = threading.Event()
        started = queue.Queue()
        body = Mock()

        @self.progress.tracked_operation
        def work(operation_id=None):
            body()
            started.put(200)
            if not release.wait(timeout=3):
                raise AssertionError("worker was not released")

        def invoke():
            barrier.wait(timeout=3)
            try:
                work(operation_id=self.first)
            except HTTPException as exc:
                started.put(exc.status_code)

        workers = [self.spawn(invoke) for _ in range(2)]
        try:
            barrier.wait(timeout=3)
            self.assertEqual(sorted(started.get(timeout=3) for _ in range(2)), [200, 409])
            body.assert_called_once()
        finally:
            release.set()
            for worker in workers:
                self.join_worker(worker)

    def test_concurrent_active_limit_is_atomic_and_recovers(self):
        barrier = threading.Barrier(18)
        release = threading.Event()
        started = queue.Queue()

        @self.progress.tracked_operation
        def work(operation_id=None):
            started.put(200)
            if not release.wait(timeout=3):
                raise AssertionError("worker was not released")

        def invoke(identifier):
            barrier.wait(timeout=3)
            try:
                work(operation_id=identifier)
            except HTTPException as exc:
                started.put(exc.status_code)

        workers = [self.spawn(lambda identifier=operation_id(number): invoke(identifier))
                   for number in range(1, 18)]
        try:
            barrier.wait(timeout=3)
            statuses = [started.get(timeout=3) for _ in range(17)]
            self.assertEqual(statuses.count(200), 16)
            self.assertEqual(statuses.count(429), 1)
            self.assertEqual(len(self.progress._records), 16)
            duplicate = self.progress.tracked_operation(Mock())
            existing_id = next(iter(self.progress._records))
            self.assert_http(409, duplicate, operation_id=existing_id)
            self.assert_http(429, duplicate, operation_id=operation_id(100))
            self.assertNotIn(operation_id(100), self.progress._records)
        finally:
            release.set()
            for worker in workers:
                self.join_worker(worker)
        self.complete(operation_id(100))
        self.assertEqual(self.progress.get_progress(operation_id(100))["status"], "completed")


if __name__ == "__main__":
    unittest.main()
