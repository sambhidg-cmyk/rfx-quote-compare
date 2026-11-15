"""On-disk cache of raw model output, one JSON file per batch.

The key covers the vendor's file bytes, the RFx lines and questions in the batch, the model
name, the prompt version and the batch size. Change any of them and the cache misses. Reruns
of the demo read from here; force=True bypasses the read but still writes the new result.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional, Union

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_DIR = ROOT / "data" / "cache" / "extract"


def make_key(*parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(json.dumps(p, sort_keys=True, default=str).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def load(cache_dir: Union[str, Path], key: str) -> Optional[Dict[str, Any]]:
    path = Path(cache_dir) / f"{key}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None  # a half-written file is treated as a miss


def save(cache_dir: Union[str, Path], key: str, payload: Dict[str, Any]) -> Path:
    d = Path(cache_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{key}.json.tmp"
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    final = d / f"{key}.json"
    tmp.replace(final)  # atomic on the same filesystem
    return final
