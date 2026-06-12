"""Global configuration and constants."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "system_clearer.db"
QUARANTINE_DIR = DATA_DIR / "quarantine"
FRONTEND_DIR = BASE_DIR / "frontend"

HOST = os.environ.get("SC_HOST", "127.0.0.1")
PORT = int(os.environ.get("SC_PORT", "9761"))

HOME = Path.home()

# Default persisted settings (stored in the DB `settings` table).
DEFAULT_SETTINGS = {
    "openai_api_key": "",
    "openai_model": "gpt-5",
    "openai_base_url": "",
    "scan_max_depth": "6",
    "largest_files_limit": "300",
    "min_node_bytes": str(5 * 1024 * 1024),  # only persist dirs/files >= 5 MB
    "default_delete_method": "trash",
}

# Token lifetime (seconds) for an authenticated session.
TOKEN_TTL = 60 * 60 * 12
