"""Define checkout paths shared by backend commands and configuration."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DATABASE = DATA_DIR / "runtime" / "helpdesk.sqlite3"
