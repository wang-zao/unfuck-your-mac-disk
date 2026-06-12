"""Small shared helpers."""


def human_size(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024.0:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} EB"


def risk_from_score(score: int) -> str:
    if score >= 8:
        return "low"
    if score >= 5:
        return "medium"
    return "high"
