from __future__ import annotations

import json
from collections.abc import Iterable
from importlib import metadata
from pathlib import Path

from pydantic import BaseModel

from polybot.models import OrderBookSnapshot


class ResearchRuntime(BaseModel):
    available: bool
    package: str = "nautilus_trader"
    version: str | None = None
    boundary: str = "offline-only; never receives signer credentials"


def inspect_nautilus_runtime() -> ResearchRuntime:
    """Report the isolated Nautilus research runtime without importing live adapters."""

    try:
        version = metadata.version("nautilus_trader")
    except metadata.PackageNotFoundError:
        return ResearchRuntime(available=False)
    return ResearchRuntime(available=True, version=version)


def export_order_books(path: Path, snapshots: Iterable[OrderBookSnapshot]) -> int:
    """Freeze normalized depth snapshots for replay in the optional research environment."""

    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for snapshot in snapshots:
            payload = {
                "schema_version": 1,
                "kind": "polymarket_order_book",
                **snapshot.model_dump(mode="json"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
            count += 1
    return count
