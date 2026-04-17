"""Photon image processing wrapper.

Python port of packages/coding-agent/src/utils/photon.ts.

The TS version wraps @silvia-odwyer/photon-node with special handling for
Bun compiled binaries. In Python, image processing would use Pillow instead.
This module provides the same interface as a stub.
"""

from __future__ import annotations

from typing import Any


async def load_photon() -> Any | None:
    """Load the photon image processing module.

    In the TS version this lazy-loads the photon-node WASM module.
    In Python this is a stub that returns None since image processing
    uses Pillow (PIL) instead. Callers should check the return value.
    """
    return None
