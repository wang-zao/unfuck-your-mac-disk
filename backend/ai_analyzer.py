"""AI analysis of disk items: GPT-5 (OpenAI) with a heuristic fallback.

For every item we return:
  explanation     - plain-language "what is this"
  score           - 1..10 deletability (10 = definitely safe to delete)
  risk            - low|medium|high
  reason          - why that score
  recommendation  - what to do
  source          - "gpt-5" or "heuristic"
"""
import json

from . import categories as cat
from . import database as db
from .util import human_size, risk_from_score

SYSTEM_PROMPT = """You are a macOS storage-cleanup expert helping a user free space on a \
nearly-full Mac. You are given a list of files/folders found on disk, each with its path, \
detected category, and size. For EACH item return:
- "explanation": one or two clear sentences explaining what this file/folder is and why it exists.
- "score": integer 1-10 for how safe it is to DELETE (10 = definitely safe, regenerated or pure junk; \
1 = do NOT delete, system-critical or irreplaceable user data).
- "risk": "low", "medium", or "high" (high risk == low score).
- "reason": short justification for the score.
- "recommendation": concise action (e.g. "Safe to delete", "Delete only if you no longer need device backups", \
"Use 'docker system prune' instead", "Keep").
Be conservative with irreplaceable data (device backups, Documents, archives, anything under user data). \
Be generous (high score) with caches, build artifacts, logs and reinstallable dependencies. \
Respond ONLY as compact JSON: {"results":[{"id":<int>,"explanation":...,"score":...,"risk":...,"reason":...,"recommendation":...}]}"""


def _heuristic(path, category, size_bytes):
    is_dir = True
    score = cat.base_score_for(path, is_dir, category)
    meta = cat.CATEGORY_BY_KEY.get(category)
    if meta:
        explanation = meta["description"]
        caveat = meta["caveat"]
        regen = meta["regenerates"]
    else:
        explanation = "Unrecognized item. Could not match a known macOS storage category."
        caveat = "Review manually before deleting."
        regen = False
    risk = risk_from_score(score)
    if score >= 8:
        rec = "Safe to delete to reclaim space."
    elif score >= 5:
        rec = "Likely deletable, but review first."
    else:
        rec = "Keep unless you are certain you no longer need it."
    reason = ("Regenerated automatically; deleting only costs rebuild/redownload time."
              if regen else "May contain data that is not automatically recreated.")
    return {
        "path": path, "category": category, "size_bytes": size_bytes, "score": score,
        "explanation": explanation, "reason": f"{reason} {caveat}".strip(),
        "risk": risk, "recommendation": rec, "source": "heuristic", "model": None,
    }


def _openai_client(settings):
    from openai import OpenAI
    key = settings.get("openai_api_key")
    if not key:
        return None
    base = settings.get("openai_base_url") or None
    return OpenAI(api_key=key, base_url=base)


def _gpt_batch(client, model, items):
    """items: list of dicts with id, path, category, size_bytes. Returns {id: result}."""
    payload = []
    for it in items:
        meta = cat.CATEGORY_BY_KEY.get(it["category"])
        payload.append({
            "id": it["id"],
            "path": it["path"],
            "category": meta["label"] if meta else (it["category"] or "unknown"),
            "category_note": meta["caveat"] if meta else "",
            "size": human_size(it["size_bytes"]),
            "size_bytes": it["size_bytes"],
        })
    user = "Analyze these items:\n" + json.dumps(payload, ensure_ascii=False)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": user}],
        response_format={"type": "json_object"},
    )
    content = resp.choices[0].message.content or "{}"
    data = json.loads(content)
    out = {}
    for r in data.get("results", []):
        try:
            rid = int(r["id"])
        except (KeyError, ValueError, TypeError):
            continue
        score = int(r.get("score", 5))
        score = max(1, min(10, score))
        out[rid] = {
            "score": score,
            "explanation": str(r.get("explanation", ""))[:1200],
            "reason": str(r.get("reason", ""))[:1200],
            "risk": str(r.get("risk", risk_from_score(score))).lower(),
            "recommendation": str(r.get("recommendation", ""))[:600],
        }
    return out


def analyze(items, force=False):
    """items: list of {path, category, size_bytes}. Returns list of analysis dicts."""
    settings = db.all_settings()
    model = settings.get("openai_model") or "gpt-5"
    results = {}

    # Reuse cache unless forced.
    paths = [it["path"] for it in items]
    cached = {} if force else db.get_analyses_for(paths)
    todo = [it for it in items if it["path"] not in cached]
    for it in items:
        if it["path"] in cached:
            results[it["path"]] = dict(cached[it["path"]])

    client = None
    used_source = "heuristic"
    ai_error = None
    if todo:
        try:
            client = _openai_client(settings)
        except Exception as exc:  # SDK/import/init issue
            client = None
            ai_error = f"OpenAI init failed: {exc}"

    if client and todo:
        for i in range(0, len(todo), 15):
            chunk = todo[i:i + 15]
            for idx, it in enumerate(chunk):
                it["id"] = idx
            try:
                gpt = _gpt_batch(client, model, chunk)
                used_source = "gpt-5"
            except Exception as exc:
                gpt = {}
                ai_error = f"{type(exc).__name__}: {exc}"
            for idx, it in enumerate(chunk):
                if idx in gpt:
                    rec = _heuristic(it["path"], it["category"], it["size_bytes"])
                    rec.update(gpt[idx])
                    rec["source"] = "gpt-5"
                    rec["model"] = model
                else:
                    rec = _heuristic(it["path"], it["category"], it["size_bytes"])
                db.upsert_analysis(rec)
                results[it["path"]] = rec
    else:
        for it in todo:
            rec = _heuristic(it["path"], it["category"], it["size_bytes"])
            db.upsert_analysis(rec)
            results[it["path"]] = rec

    ordered = [results[p] for p in paths if p in results]
    return {"results": ordered, "source": used_source, "ai_error": ai_error}


def test_connection(settings) -> dict:
    """Validate the OpenAI key/model with a tiny request."""
    try:
        client = _openai_client(settings)
    except Exception as exc:
        return {"ok": False, "error": f"SDK error: {exc}"}
    if client is None:
        return {"ok": False, "error": "No API key set."}
    model = settings.get("openai_model") or "gpt-5"
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": "Reply with the single word: ok."},
                      {"role": "user", "content": "ping"}],
        )
        txt = (resp.choices[0].message.content or "").strip()
        return {"ok": True, "model": model, "reply": txt[:60]}
    except Exception as exc:
        return {"ok": False, "model": model, "error": f"{type(exc).__name__}: {exc}"}
