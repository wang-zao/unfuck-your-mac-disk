"""FastAPI application: API endpoints + static frontend serving."""
import json
import os
import shutil
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ai_analyzer, auth
from . import categories as cat
from . import cleaner, config
from . import database as db
from . import scanner
from .util import human_size

app = FastAPI(title="System Disk Space Manager", version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    db.init_db()


# --------------------------- auth helpers ---------------------------
def require_auth(authorization: Optional[str] = Header(None)):
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not auth.verify_token(token):
        raise HTTPException(status_code=401, detail="Authentication required.")
    return True


# --------------------------- models ---------------------------
class PinReq(BaseModel):
    pin: str


class ChangePinReq(BaseModel):
    old_pin: str
    new_pin: str


class ScanReq(BaseModel):
    mode: str = "system_data"
    path: Optional[str] = None
    keys: Optional[list] = None


class AnalyzeReq(BaseModel):
    paths: Optional[list] = None
    scan_id: Optional[int] = None
    top: int = 40
    only_dirs: bool = True
    force: bool = False


class DeleteReq(BaseModel):
    paths: list
    method: str = "trash"


class SettingsReq(BaseModel):
    openai_api_key: Optional[str] = None
    openai_model: Optional[str] = None
    openai_base_url: Optional[str] = None
    scan_max_depth: Optional[int] = None
    largest_files_limit: Optional[int] = None
    min_node_bytes: Optional[int] = None
    default_delete_method: Optional[str] = None


# --------------------------- public ---------------------------
@app.get("/api/health")
def health():
    return {"ok": True, "version": "1.0.0", "configured": auth.is_configured()}


@app.get("/api/auth/status")
def auth_status():
    return {"configured": auth.is_configured()}


@app.post("/api/auth/setup")
def auth_setup(req: PinReq):
    if auth.is_configured():
        raise HTTPException(400, "Already configured. Use login.")
    try:
        auth.setup_pin(req.pin)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"token": auth.create_token()}


@app.post("/api/auth/login")
def auth_login(req: PinReq):
    if not auth.is_configured():
        raise HTTPException(400, "Not configured. Set up a PIN first.")
    if not auth.verify_pin(req.pin):
        raise HTTPException(401, "Incorrect PIN.")
    return {"token": auth.create_token()}


@app.post("/api/auth/change")
def auth_change(req: ChangePinReq, _=Depends(require_auth)):
    try:
        auth.change_pin(req.old_pin, req.new_pin)
    except (PermissionError, ValueError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# --------------------------- settings ---------------------------
@app.get("/api/settings")
def get_settings(_=Depends(require_auth)):
    s = db.all_settings()
    key = s.get("openai_api_key", "")
    return {
        "openai_api_key_set": bool(key),
        "openai_api_key_masked": (key[:3] + "..." + key[-4:]) if len(key) > 10 else "",
        "openai_model": s.get("openai_model", "gpt-5"),
        "openai_base_url": s.get("openai_base_url", ""),
        "scan_max_depth": int(s.get("scan_max_depth", "6")),
        "largest_files_limit": int(s.get("largest_files_limit", "300")),
        "min_node_bytes": int(s.get("min_node_bytes", str(5 * 1024 * 1024))),
        "default_delete_method": s.get("default_delete_method", "trash"),
    }


@app.post("/api/settings")
def update_settings(req: SettingsReq, _=Depends(require_auth)):
    data = req.dict(exclude_none=True)
    for k, v in data.items():
        db.set_setting(k, str(v))
    return get_settings()


@app.post("/api/settings/test-openai")
def test_openai(_=Depends(require_auth)):
    return ai_analyzer.test_connection(db.all_settings())


# --------------------------- catalog / disk ---------------------------
@app.get("/api/categories")
def categories(_=Depends(require_auth)):
    return {"categories": cat.CATEGORIES, "default_keys": cat.DEFAULT_SCAN_KEYS}


@app.get("/api/targets")
def targets(_=Depends(require_auth)):
    out = []
    for c in cat.CATEGORIES:
        existing = [p for p in c["paths"] if os.path.exists(p)]
        out.append({
            "key": c["key"], "label": c["label"], "icon": c.get("icon"),
            "base_score": c["base_score"], "description": c["description"],
            "caveat": c["caveat"], "paths": c["paths"], "existing_paths": existing,
            "exists": bool(existing),
            "default": c["key"] in cat.DEFAULT_SCAN_KEYS,
        })
    return {"targets": out}


@app.get("/api/disk")
def disk(_=Depends(require_auth)):
    vols = []
    seen = set()
    candidates = ["/", "/System/Volumes/Data"]
    for d in sorted(os.listdir("/Volumes")) if os.path.isdir("/Volumes") else []:
        candidates.append(os.path.join("/Volumes", d))
    for path in candidates:
        try:
            st = os.statvfs(path)
            dev = st.f_fsid
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
        except OSError:
            continue
        key = (round(total / 1e6), round(free / 1e6))
        if key in seen or total == 0:
            continue
        seen.add(key)
        used = total - free
        vols.append({
            "mount": path,
            "label": "Macintosh HD" if path in ("/", "/System/Volumes/Data") else os.path.basename(path),
            "total": total, "used": used, "free": free,
            "used_human": human_size(used), "free_human": human_size(free),
            "total_human": human_size(total),
            "percent": round(used / total * 100, 1) if total else 0,
        })
    return {"volumes": vols}


# --------------------------- scanning ---------------------------
@app.post("/api/scan")
def start_scan(req: ScanReq, _=Depends(require_auth)):
    try:
        scan_id = scanner.start_scan(req.mode, req.path, req.keys)
    except (RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))
    return {"scan_id": scan_id, "status": "running"}


@app.get("/api/scan/status")
def scan_status(_=Depends(require_auth)):
    return scanner.get_state()


@app.post("/api/scan/cancel")
def scan_cancel(_=Depends(require_auth)):
    scanner.request_cancel()
    return {"ok": True}


@app.get("/api/scans")
def scans(_=Depends(require_auth)):
    rows = [dict(r) for r in db.list_scans()]
    for r in rows:
        r["total_human"] = human_size(r.get("total_bytes") or 0)
    return {"scans": rows, "latest": (lambda x: dict(x) if x else None)(db.latest_scan())}


def _node_dict(r):
    d = dict(r)
    d["size_human"] = human_size(d.get("size_bytes") or 0)
    meta = cat.CATEGORY_BY_KEY.get(d.get("category"))
    d["category_label"] = meta["label"] if meta else (d.get("category") or "Uncategorized")
    return d


@app.get("/api/scan/{scan_id}/summary")
def scan_summary(scan_id: int, _=Depends(require_auth)):
    s = db.get_scan(scan_id)
    if not s:
        raise HTTPException(404, "No such scan.")
    breakdown = scanner.category_breakdown(scan_id)
    cats = []
    for key, byts in sorted(breakdown.items(), key=lambda x: -x[1]):
        meta = cat.CATEGORY_BY_KEY.get(key)
        cats.append({
            "key": key, "bytes": byts, "human": human_size(byts),
            "label": meta["label"] if meta else key.replace("_", " ").title(),
            "base_score": meta["base_score"] if meta else None,
        })
    return {
        "scan": dict(s),
        "total_bytes": s["total_bytes"], "total_human": human_size(s["total_bytes"]),
        "total_files": s["total_files"], "total_dirs": s["total_dirs"],
        "categories": cats,
    }


@app.get("/api/scan/{scan_id}/tree")
def scan_tree(scan_id: int, parent: Optional[str] = None, limit: int = 200,
              _=Depends(require_auth)):
    rows = db.children(scan_id, parent, limit)
    return {"nodes": [_node_dict(r) for r in rows]}


def _build_tree(scan_id, parent, depth, breadth):
    rows = db.children(scan_id, parent, breadth)
    out = []
    for r in rows:
        node = {"name": r["name"], "path": r["path"], "value": r["size_bytes"],
                "category": r["category"], "is_dir": r["is_dir"]}
        if depth > 0 and r["is_dir"]:
            kids = _build_tree(scan_id, r["path"], depth - 1, breadth)
            if kids:
                node["children"] = kids
        out.append(node)
    return out


@app.get("/api/scan/{scan_id}/treemap")
def scan_treemap(scan_id: int, depth: int = 3, breadth: int = 12,
                 _=Depends(require_auth)):
    if not db.get_scan(scan_id):
        raise HTTPException(404, "No such scan.")
    return {"tree": _build_tree(scan_id, None, depth, breadth)}


@app.get("/api/scan/{scan_id}/items")
def scan_items(scan_id: int, top: int = 300, category: Optional[str] = None,
               only_dirs: bool = False, _=Depends(require_auth)):
    rows = db.top_items(scan_id, top, category, only_dirs)
    items = [_node_dict(r) for r in rows]
    analyses = db.get_analyses_for([i["path"] for i in items])
    for i in items:
        a = analyses.get(i["path"])
        if a:
            i["analysis"] = {k: a[k] for k in ("score", "explanation", "reason",
                                               "risk", "recommendation", "source")}
    return {"items": items}


# --------------------------- AI analysis ---------------------------
@app.post("/api/analyze")
def analyze(req: AnalyzeReq, _=Depends(require_auth)):
    items = []
    if req.paths:
        # Look up size/category from the most recent scan's nodes.
        latest = db.latest_scan()
        sid = req.scan_id or (latest["id"] if latest else None)
        for p in req.paths:
            row = None
            if sid:
                row = db.connect().execute(
                    "SELECT * FROM nodes WHERE scan_id=? AND path=? ORDER BY size_bytes DESC LIMIT 1",
                    (sid, p)).fetchone()
            if row:
                items.append({"path": p, "category": row["category"],
                              "size_bytes": row["size_bytes"]})
            else:
                isdir = os.path.isdir(p)
                items.append({"path": p, "category": cat.classify(p, isdir),
                              "size_bytes": cleaner.dir_size(p) if os.path.exists(p) else 0})
    elif req.scan_id:
        rows = db.top_items(req.scan_id, req.top, None, req.only_dirs)
        items = [{"path": r["path"], "category": r["category"],
                  "size_bytes": r["size_bytes"]} for r in rows]
    else:
        raise HTTPException(400, "Provide paths or scan_id.")

    if not items:
        return {"results": [], "source": "none", "ai_error": None}
    res = ai_analyzer.analyze(items, force=req.force)
    for r in res["results"]:
        r["size_human"] = human_size(r.get("size_bytes") or 0)
    return res


@app.get("/api/analysis")
def get_analysis(path: str, _=Depends(require_auth)):
    row = db.get_analysis(path)
    return {"analysis": dict(row) if row else None}


# --------------------------- deletion ---------------------------
@app.post("/api/delete")
def delete(req: DeleteReq, _=Depends(require_auth)):
    if not req.paths:
        raise HTTPException(400, "No paths given.")
    res = cleaner.delete_many(req.paths, req.method)
    res["bytes_freed_human"] = human_size(res["bytes_freed"])
    return res


@app.get("/api/deletions")
def deletions(_=Depends(require_auth)):
    rows = [dict(r) for r in db.list_deletions()]
    for r in rows:
        r["size_human"] = human_size(r.get("size_bytes") or 0)
    return {"deletions": rows}


@app.post("/api/deletions/{del_id}/restore")
def restore(del_id: int, _=Depends(require_auth)):
    try:
        return cleaner.restore(del_id)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/quarantine/empty")
def empty_quarantine(_=Depends(require_auth)):
    res = cleaner.empty_quarantine()
    res["bytes_freed_human"] = human_size(res["bytes_freed"])
    return res


# --------------------------- frontend ---------------------------
@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


@app.get("/")
def index():
    return FileResponse(str(config.FRONTEND_DIR / "index.html"))


if config.FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIR), html=True), name="static")
