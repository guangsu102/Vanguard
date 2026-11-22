"""Declared physical envelope of every optional V5 evidence component.

No component may introduce a second persisted payload or diagnostic path. The
business database and SDK protocol recovery database remain original durable
workflows, never used as an overflow cache or optional diagnostic destination.
"""
from __future__ import annotations

import stat
from collections.abc import Mapping
from pathlib import Path

MIB = 1024 * 1024
HARD_LIMIT_BYTES = 1024 * MIB
FILES = {
    "evidence": ("review-evidence-v5.bin", 537006080),
    "metrics": ("review-read-metrics-v1.bin", 16261120),
    "diagnostics": ("review-diagnostics-v1.bin", 2 * MIB),
}
# Covers directory blocks and allocation/extent bookkeeping measured by stat.
# Each file writer separately refuses size growth, links and unexpected files.
FILESYSTEM_RESERVE_BYTES = len(FILES) * MIB
DECLARED_PEAK_BYTES = sum(size for _name, size in FILES.values()) + FILESYSTEM_RESERVE_BYTES
assert DECLARED_PEAK_BYTES < HARD_LIMIT_BYTES


def verify(paths: Mapping[str, str | Path]) -> dict:
    """Pure read-only verification; never creates, removes or repairs anything."""
    result = {"ok": False, "hard_limit_bytes": HARD_LIMIT_BYTES,
              "declared_peak_bytes": DECLARED_PEAK_BYTES, "allocated_bytes": 0,
              "logical_bytes": 0, "reason": "unverified"}
    try:
        if set(paths) != set(FILES):
            raise ValueError("component_path_set_incomplete")
        roots = {key: Path(value) for key, value in paths.items()}
        if any(not root.is_absolute() or root.is_symlink() for root in roots.values()):
            raise ValueError("component_path_unverified")
        resolved = [root.resolve() for root in roots.values()]
        if len(set(resolved)) != len(FILES) or any(
                left != right and left in right.parents for left in resolved for right in resolved):
            raise ValueError("component_paths_overlap")
        for key, root in roots.items():
            filename, expected = FILES[key]
            if not root.exists():
                continue  # The future file's full allocation is already reserved.
            for entry in root.iterdir():
                info = entry.lstat()
                if entry.name != filename or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("unexpected_component_artifact")
                if info.st_size != expected:
                    raise ValueError("component_file_size_mismatch")
                allocated = getattr(info, "st_blocks", 0) * 512 or info.st_size
                if allocated > expected + MIB:
                    raise ValueError("component_allocation_reserve_exceeded")
                result["logical_bytes"] += info.st_size
                result["allocated_bytes"] += allocated
            info = root.stat()
            result["allocated_bytes"] += getattr(info, "st_blocks", 0) * 512
        if result["allocated_bytes"] > DECLARED_PEAK_BYTES:
            raise ValueError("component_physical_limit_exceeded")
        result.update(ok=True, reason="fixed_component_budget_verified")
    except (OSError, ValueError, TypeError) as exc:
        result["reason"] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
    return result
