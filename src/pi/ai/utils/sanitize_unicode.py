"""Unicode sanitization — Python port of packages/ai/src/utils/sanitize-unicode.ts.

In Python 3, strings are Unicode by default and don't normally contain
unpaired surrogates. However, surrogates can appear when reading from
external sources (e.g., C extensions, files decoded with 'surrogatepass').
"""

from __future__ import annotations

import re

# Match unpaired surrogates
_SURROGATE_PATTERN = re.compile(
    r"[\ud800-\udbff](?![\udc00-\udfff])"  # High surrogate not followed by low
    r"|(?<![\ud800-\udbff])[\udc00-\udfff]",  # Low surrogate not preceded by high
)


def sanitize_surrogates(text: str) -> str:
    """Remove unpaired Unicode surrogate characters from a string.

    Valid emoji and other characters outside the Basic Multilingual Plane
    use properly paired surrogates and will NOT be affected.
    """
    return _SURROGATE_PATTERN.sub("", text)
