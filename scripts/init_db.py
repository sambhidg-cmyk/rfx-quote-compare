"""Create or migrate the local SQLite database.

Usage (from the project root):
    python3 scripts/init_db.py [path/to/db.sqlite]

Defaults to the DATABASE_PATH env var, falling back to data/rfx.db.
"""

import os
import sys
from pathlib import Path

# Make the project root importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.db import init_db  # noqa: E402


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DATABASE_PATH", "data/rfx.db")
    conn = init_db(path)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    conn.close()
    print(f"Initialised {path}: {len(tables)} tables")
    for t in tables:
        print(f"  - {t}")


if __name__ == "__main__":
    main()
