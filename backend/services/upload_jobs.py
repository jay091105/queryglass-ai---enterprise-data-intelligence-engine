"""In-process background upload jobs with observable, server-reported progress."""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}
_EXECUTOR_LOCK = threading.Lock()
_EXECUTOR: ThreadPoolExecutor | None = ThreadPoolExecutor(max_workers=1, thread_name_prefix="queryglass-upload")
_RETENTION_SECONDS = 60 * 60
_MAX_RETAINED_JOBS = 100


def create_upload_job(filename: str, total_bytes: int) -> str:
    now = time.monotonic()
    job_id = uuid.uuid4().hex
    with _LOCK:
        expired = [
            job_id
            for job_id, job in _JOBS.items()
            if job["status"] in {"completed", "failed"}
            and now - job["_updated_monotonic"] > _RETENTION_SECONDS
        ]
        for expired_id in expired:
            del _JOBS[expired_id]
        if len(_JOBS) >= _MAX_RETAINED_JOBS:
            completed = sorted(
                (
                    (job["_updated_monotonic"], job_id)
                    for job_id, job in _JOBS.items()
                    if job["status"] in {"completed", "failed"}
                )
            )
            while len(_JOBS) >= _MAX_RETAINED_JOBS and completed:
                _, job_id = completed.pop(0)
                del _JOBS[job_id]
        if len(_JOBS) >= _MAX_RETAINED_JOBS:
            raise RuntimeError("The upload job queue is full. Wait for current uploads to finish.")
        _JOBS[job_id] = {
            "job_id": job_id,
            "filename": filename,
            "total_bytes": total_bytes,
            "status": "queued",
            "stage": "queued",
            "processed_rows": 0,
            "total_rows": None,
            "elapsed_seconds": 0.0,
            "error": None,
            "result": None,
            "_started_monotonic": now,
            "_updated_monotonic": now,
        }
    return job_id


def update_upload_job(job_id: str, **updates: Any) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.update(updates)
        job["_updated_monotonic"] = time.monotonic()


def get_upload_job(job_id: str) -> dict[str, Any] | None:
    now = time.monotonic()
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return None
        snapshot = {key: value for key, value in job.items() if not key.startswith("_")}
        snapshot["elapsed_seconds"] = round(now - job["_started_monotonic"], 1)
        return snapshot


def submit_upload_job(function: Any, *args: Any) -> None:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="queryglass-upload")
        executor = _EXECUTOR
    executor.submit(function, *args)


def shutdown_upload_jobs(wait: bool = True) -> None:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is not None:
            _EXECUTOR.shutdown(wait=wait, cancel_futures=True)
            _EXECUTOR = None
