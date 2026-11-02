import re
import threading
import time
from copy import deepcopy
from functools import wraps

from fastapi import HTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException


_COMPLETED_TTL_SECONDS = 900
_MAX_RECORDS = 128
_MAX_ACTIVE = 16
_DETAIL_KEYS = frozenset({
    "host", "received_bytes", "total_bytes", "attempt", "timeout_seconds",
})
_records = {}
_lock = threading.RLock()
_context = threading.local()


def _validate_id(operation_id):
    if not isinstance(operation_id, str) or re.fullmatch(r"[0-9a-f]{32}", operation_id) is None:
        raise HTTPException(status_code=400, detail="operation_id 必须为32位ASCII小写十六进制字符串")


def _prune(now):
    expired = [
        operation_id for operation_id, record in _records.items()
        if record["finished_at"] is not None
        and now - record["finished_at"] >= _COMPLETED_TTL_SECONDS
    ]
    for operation_id in expired:
        del _records[operation_id]


def _start(operation_id):
    with _lock:
        now = time.monotonic()
        _prune(now)
        if operation_id in _records:
            raise HTTPException(status_code=409, detail="operation_id 已使用")
        active = sum(record["status"] == "running" for record in _records.values())
        if active >= _MAX_ACTIVE:
            raise HTTPException(status_code=429, detail="同时进行的操作过多，请稍后重试")
        if len(_records) >= _MAX_RECORDS:
            finished = [
                operation_id for operation_id, record in _records.items()
                if record["finished_at"] is not None
            ]
            if not finished:
                raise HTTPException(status_code=429, detail="操作记录已满，请稍后重试")
            oldest = min(finished, key=lambda key: _records[key]["finished_at"])
            del _records[oldest]
        _records[operation_id] = {
            "id": operation_id,
            "status": "running",
            "message": "等待其他写操作完成",
            "started_at": now,
            "stage_started_at": now,
            "finished_at": None,
            "details": {},
        }


def _finish(operation_id, status, message, http_status=None):
    with _lock:
        now = time.monotonic()
        record = _records[operation_id]
        if message != record["message"]:
            record["stage_started_at"] = now
        record.update(status=status, message=message, finished_at=now, details={})
        if http_status is not None:
            record["http_status"] = http_status


def tracked_operation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        operation_id = kwargs.get("operation_id")
        if operation_id is None:
            return function(*args, **kwargs)
        _validate_id(operation_id)
        _start(operation_id)
        previous_id = getattr(_context, "operation_id", None)
        _context.operation_id = operation_id
        try:
            result = function(*args, **kwargs)
            message = "操作完成"
            if isinstance(result, dict) and result.get("warnings"):
                message = "操作完成，但有步骤需要核对"
            _finish(operation_id, "completed", message)
            return result
        except StarletteHTTPException as exc:
            _finish(operation_id, "failed", str(exc.detail)[:2000], exc.status_code)
            raise
        except BaseException:
            _finish(operation_id, "failed", "操作失败，请查看服务日志")
            raise
        finally:
            if previous_id is None:
                del _context.operation_id
            else:
                _context.operation_id = previous_id

    return wrapped


def update_progress(message, **details):
    operation_id = getattr(_context, "operation_id", None)
    if operation_id is None:
        return
    with _lock:
        record = _records.get(operation_id)
        if record is None or record["status"] != "running":
            return
        if message != record["message"]:
            record["stage_started_at"] = time.monotonic()
            record["details"] = {}
        record["message"] = message
        record["details"].update(deepcopy({
            key: value for key, value in details.items() if key in _DETAIL_KEYS
        }))


def get_progress(operation_id):
    _validate_id(operation_id)
    with _lock:
        now = time.monotonic()
        _prune(now)
        record = _records.get(operation_id)
        if record is None:
            raise HTTPException(status_code=404, detail="操作记录不存在或已过期")
        end = record["finished_at"] if record["finished_at"] is not None else now
        progress = {
            "id": record["id"],
            "status": record["status"],
            "message": record["message"],
            "elapsed_seconds": end - record["started_at"],
            "stage_elapsed_seconds": end - record["stage_started_at"],
            "details": deepcopy(record["details"]),
        }
        if "http_status" in record:
            progress["http_status"] = record["http_status"]
        return progress
