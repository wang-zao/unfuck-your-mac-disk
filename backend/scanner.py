"""Threaded disk-tree scanner with live progress and category attribution."""
import heapq
import json
import os
import threading
import time
from collections import defaultdict

from . import categories as cat
from . import config
from . import database as db

_lock = threading.Lock()
_state = {
    "status": "idle",      # idle|running|done|error|cancelled
    "scan_id": None,
    "mode": None,
    "current": "",
    "bytes": 0,
    "files": 0,
    "dirs": 0,
    "roots_total": 0,
    "roots_done": 0,
    "started_at": None,
    "error": None,
}
_cancel = threading.Event()


def get_state() -> dict:
    with _lock:
        s = dict(_state)
    s["elapsed"] = round(time.time() - s["started_at"], 1) if s.get("started_at") else 0
    return s


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def is_running() -> bool:
    with _lock:
        return _state["status"] == "running"


def request_cancel() -> None:
    _cancel.set()


def _physical_size(st) -> int:
    # Actual allocated disk space (APFS), not logical file size.
    blocks = getattr(st, "st_blocks", None)
    if blocks is not None:
        return blocks * 512
    return st.st_size


def _walk(root_dev, path, depth, max_depth, min_bytes, eff_cat,
          nodes, largest, cat_bytes, counters, store_limit):
    """Recursively measure `path`. Returns (size_bytes, file_count, dir_count)."""
    if _cancel.is_set():
        return 0, 0, 0

    total = 0
    files = 0
    dirs = 0
    try:
        entries = list(os.scandir(path))
    except (PermissionError, FileNotFoundError, NotADirectoryError, OSError):
        return 0, 0, 0

    for entry in entries:
        if _cancel.is_set():
            break
        try:
            st = entry.stat(follow_symlinks=False)
        except (OSError, ValueError):
            continue

        # Stay on the same physical volume; don't wander into other mounts.
        if root_dev is not None and getattr(st, "st_dev", root_dev) != root_dev:
            continue

        is_link = entry.is_symlink()
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            is_dir = False

        if is_dir and not is_link:
            child_cat = cat.classify(entry.path, True) or eff_cat
            sz, fc, dc = _walk(root_dev, entry.path, depth + 1, max_depth, min_bytes,
                               child_cat, nodes, largest, cat_bytes, counters, store_limit)
            total += sz
            files += fc
            dirs += dc + 1

            store = (depth + 1) <= max_depth and (sz >= min_bytes or depth + 1 <= 2)
            if store and len(nodes) < store_limit:
                nodes.append((entry.path, path, entry.name, child_cat, 1,
                              depth + 1, sz, fc, dc))
        else:
            sz = _physical_size(st)
            total += sz
            files += 1
            counters["files"] += 1
            counters["bytes"] += sz
            cat_bytes[eff_cat] += sz
            if sz >= min_bytes:
                item = (sz, entry.path, eff_cat, path)
                if len(largest) < counters["largest_n"]:
                    heapq.heappush(largest, item)
                elif sz > largest[0][0]:
                    heapq.heapreplace(largest, item)

            counters["since_update"] += 1
            if counters["since_update"] >= 400:
                counters["since_update"] = 0
                _set(bytes=counters["bytes"], files=counters["files"],
                     dirs=counters["dirs"], current=path)

    counters["dirs"] += dirs
    return total, files, dirs


def _run(scan_id, mode, roots_with_cat, max_depth, min_bytes, largest_n):
    nodes: list[tuple] = []
    largest: list[tuple] = []
    cat_bytes: dict = defaultdict(int)
    counters = {"bytes": 0, "files": 0, "dirs": 0, "since_update": 0,
                "largest_n": largest_n}
    grand = 0
    try:
        for idx, (category, root) in enumerate(roots_with_cat):
            if _cancel.is_set():
                break
            _set(current=root, roots_done=idx)
            try:
                rst = os.lstat(root)
                root_dev = getattr(rst, "st_dev", None)
            except OSError:
                continue
            eff_cat = category or cat.classify(root, True)
            sz, fc, dc = _walk(root_dev, root, 0, max_depth, min_bytes, eff_cat,
                               nodes, largest, cat_bytes, counters, store_limit=20000)
            grand += sz
            # Root node (always stored, depth 0).
            nodes.append((root, None, os.path.basename(root.rstrip("/")) or root,
                          eff_cat, 1, 0, sz, fc, dc))

        # Persist directory nodes.
        rows = [(scan_id, p, parent, name, c, isdir, depth, size, fc, dc)
                for (p, parent, name, c, isdir, depth, size, fc, dc) in nodes]
        # Add the biggest individual files as leaf nodes too.
        for sz, fpath, fcat, parent in sorted(largest, reverse=True):
            rows.append((scan_id, fpath, parent, os.path.basename(fpath), fcat, 0,
                         1, sz, 0, 0))
        db.insert_nodes(rows)

        totals = {"bytes": counters["bytes"], "files": counters["files"],
                  "dirs": counters["dirs"]}
        db.set_setting(f"scan_{scan_id}_categories",
                       json.dumps({k or "uncategorized": v for k, v in cat_bytes.items()}))
        status = "cancelled" if _cancel.is_set() else "done"
        db.finish_scan(scan_id, status, totals)
        _set(status=status, bytes=counters["bytes"], files=counters["files"],
             dirs=counters["dirs"], roots_done=len(roots_with_cat), current="")
    except Exception as exc:  # pragma: no cover - defensive
        db.finish_scan(scan_id, "error", {"bytes": counters["bytes"]}, str(exc))
        _set(status="error", error=str(exc))


def start_scan(mode: str, path: str = None, keys: list = None) -> int:
    """Begin a scan in a background thread. Returns the scan id."""
    if is_running():
        raise RuntimeError("A scan is already running.")

    max_depth = int(db.get_setting("scan_max_depth", "6"))
    min_bytes = int(db.get_setting("min_node_bytes", str(5 * 1024 * 1024)))
    largest_n = int(db.get_setting("largest_files_limit", "300"))

    if mode == "path":
        if not path:
            raise ValueError("path is required for a path scan.")
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.exists(path):
            raise ValueError(f"Path does not exist: {path}")
        roots_with_cat = [(cat.classify(path, os.path.isdir(path)), path)]
        label = f"Path: {path}"
    else:  # system_data
        scan_keys = keys or cat.DEFAULT_SCAN_KEYS
        roots_with_cat = cat.existing_targets(scan_keys)
        label = "macOS System Data"
        if not roots_with_cat:
            raise ValueError("None of the selected System Data locations exist.")

    roots = [r for _, r in roots_with_cat]
    scan_id = db.create_scan(label, mode, json.dumps(roots))

    _cancel.clear()
    with _lock:
        _state.update({"status": "running", "scan_id": scan_id, "mode": mode,
                       "current": "", "bytes": 0, "files": 0, "dirs": 0,
                       "roots_total": len(roots_with_cat), "roots_done": 0,
                       "started_at": time.time(), "error": None})

    t = threading.Thread(target=_run, args=(scan_id, mode, roots_with_cat,
                                            max_depth, min_bytes, largest_n),
                         daemon=True)
    t.start()
    return scan_id


def category_breakdown(scan_id: int) -> dict:
    raw = db.get_setting(f"scan_{scan_id}_categories")
    return json.loads(raw) if raw else {}
