"""Safe deletion engine: protected-path guard, Trash, quarantine, restore."""
import os
import shutil
import time
import uuid

from . import categories as cat
from . import config
from . import database as db


def dir_size(path: str) -> int:
    if os.path.isfile(path) or os.path.islink(path):
        try:
            return os.lstat(path).st_blocks * 512
        except OSError:
            return 0
    total = 0
    for root, dirs, files in os.walk(path, topdown=True, followlinks=False):
        for f in files:
            try:
                st = os.lstat(os.path.join(root, f))
                total += getattr(st, "st_blocks", 0) * 512
            except OSError:
                continue
    return total


def _guard(path: str) -> str:
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(path) and not os.path.islink(path):
        raise FileNotFoundError(f"Path no longer exists: {path}")
    if cat.is_protected(path):
        raise PermissionError(f"Refused: '{path}' is protected and cannot be deleted.")
    # Never allow deleting our own app/data directory.
    app = str(config.BASE_DIR)
    if path == app or path.startswith(app + "/"):
        raise PermissionError("Refused: cannot delete the application's own files.")
    return path


def _to_trash(path: str) -> None:
    from send2trash import send2trash
    send2trash(path)


def _to_quarantine(path: str) -> str:
    config.QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:8]
    dest = config.QUARANTINE_DIR / f"{token}__{os.path.basename(path.rstrip('/'))}"
    shutil.move(path, str(dest))
    return str(dest)


def _permanent(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=False)
    else:
        os.remove(path)


def delete_path(path: str, method: str = "trash") -> dict:
    """Delete one path with the chosen method. Records an audit row."""
    safe = _guard(path)
    size = dir_size(safe)
    quarantine_path = None
    try:
        if method == "trash":
            _to_trash(safe)
        elif method == "quarantine":
            quarantine_path = _to_quarantine(safe)
        elif method == "permanent":
            _permanent(safe)
        else:
            raise ValueError(f"Unknown method: {method}")
    except Exception as exc:
        db.record_deletion({"path": safe, "size_bytes": size, "method": method,
                            "status": "failed", "note": str(exc)})
        raise

    del_id = db.record_deletion({
        "path": safe, "size_bytes": size, "method": method, "status": "done",
        "quarantine_path": quarantine_path,
        "note": f"Reclaimed {size} bytes via {method}.",
    })
    return {"id": del_id, "path": safe, "size_bytes": size, "method": method,
            "quarantine_path": quarantine_path}


def delete_many(paths: list, method: str = "trash") -> dict:
    results, errors, freed = [], [], 0
    for p in paths:
        try:
            r = delete_path(p, method)
            results.append(r)
            freed += r["size_bytes"]
        except Exception as exc:
            errors.append({"path": p, "error": str(exc)})
    return {"deleted": results, "errors": errors, "bytes_freed": freed,
            "count": len(results)}


def restore(del_id: int) -> dict:
    row = db.get_deletion(del_id)
    if not row:
        raise FileNotFoundError("No such deletion record.")
    if row["method"] != "quarantine":
        raise ValueError("Only quarantined items can be restored automatically. "
                         "Trash items: restore from Finder's Trash.")
    if row["status"] == "restored":
        raise ValueError("Already restored.")
    qpath = row["quarantine_path"]
    if not qpath or not os.path.exists(qpath):
        raise FileNotFoundError("Quarantined data is gone (emptied).")
    dest = row["path"]
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(qpath, dest)
    db.mark_restored(del_id)
    return {"restored": dest}


def empty_quarantine() -> dict:
    freed = 0
    removed = 0
    if config.QUARANTINE_DIR.exists():
        for child in config.QUARANTINE_DIR.iterdir():
            freed += dir_size(str(child))
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink()
                removed += 1
            except OSError:
                continue
    return {"removed": removed, "bytes_freed": freed}
