"""HTTP proxy setup — Python port of packages/ai/src/utils/http-proxy.ts.

Python's httpx natively supports HTTP_PROXY / HTTPS_PROXY / NO_PROXY
environment variables, so no special setup is needed.

This module exists for API parity. Import it if you want to ensure
httpx proxy support documentation is referenced.
"""

from __future__ import annotations

# httpx automatically reads HTTP_PROXY, HTTPS_PROXY, ALL_PROXY, NO_PROXY
# from environment variables. No manual setup required.
# See: https://www.python-httpx.org/advanced/proxies/
