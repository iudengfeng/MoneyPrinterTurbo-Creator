from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import store

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="creator")
_lock = threading.Lock()
_recovered_roots = set()


def _recover():
    root = str(store.data_root())
    with _lock:
        if root in _recovered_roots:
            return
        for row in store.list_records("jobs"):
            if row.get("state") in {"running", "queued"}:
                store.update_record("jobs", row["id"], {"state": "interrupted", "message": "程序已重新启动，请检查结果后重新提交。发布任务请先核查平台作品列表。"})
        _recovered_roots.add(root)


def submit(label, operation, *args, **kwargs) -> str:
    _recover()
    if sum(j.get("state") in {"queued", "running"} for j in store.list_records("jobs")) >= 20:
        raise ValueError("任务队列已满，请等当前任务完成。")
    ident = store.new_id()
    store.save_record("jobs", ident, {"label": label, "state": "queued", "progress": 0, "message": "排队中"})

    def work():
        def progress(message, percent=None):
            changes = {"message": str(message)}
            if percent is not None:
                changes["progress"] = max(0, min(100, float(percent)))
            store.update_record("jobs", ident, changes)

        store.update_record("jobs", ident, {"state": "running", "message": "开始处理"})
        try:
            result = operation(*args, progress=progress, **kwargs)
            state = "done"
            message = "已完成"
            if isinstance(result, dict):
                status = result.get("status")
                if status in {"needs_user", "waiting_login", "submission_unknown"}:
                    state = "needs_user"
                elif status == "failed_before_submit":
                    state = "failed"
                message = result.get("message") or message
            store.update_record("jobs", ident, {"state": state, "progress": 100, "message": message, "result": result, "ended_at": datetime.now(timezone.utc).isoformat()})
        except Exception as exc:
            store.update_record("jobs", ident, {"state": "failed", "message": str(exc)[:1600], "ended_at": datetime.now(timezone.utc).isoformat()})

    _executor.submit(work)
    return ident


def list_jobs():
    _recover()
    return store.list_records("jobs")


def get_job(ident):
    _recover()
    return store.get_record("jobs", ident)
