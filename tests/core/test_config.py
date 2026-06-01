"""Behavioral tests for rolemesh.core.config.

The module computes its constants from os.environ at import time, so we
exercise the real parsing/clamping/fallback logic by importing it in a
subprocess under a controlled environment and reading back the resulting
values. This finds the bugs that matter — a removed clamp, a loosened
boolean parse, a broken timezone fallback — which the previous
``assert POLL_INTERVAL == 2.0`` constant-reads could not.
"""

from __future__ import annotations

import json
import subprocess
import sys
from functools import cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent

# Imported in the subprocess: optionally neutralize the host's system
# timezone files so TIMEZONE resolution depends only on the TZ env var,
# then dump the parsed constants as JSON.
_SCRIPT = r"""
import os, json
if os.environ.get("_TEST_NO_SYSTEM_TZ") == "1":
    import pathlib
    _orig_read = pathlib.Path.read_text
    def _no_etc_timezone(self, *a, **k):
        if str(self) == "/etc/timezone":
            raise OSError("patched: no /etc/timezone")
        return _orig_read(self, *a, **k)
    pathlib.Path.read_text = _no_etc_timezone
    def _no_localtime(*a, **k):
        raise OSError("patched: no /etc/localtime")
    os.readlink = _no_localtime
import rolemesh.core.config as c
print(json.dumps({
    "ASSISTANT_NAME": c.ASSISTANT_NAME,
    "ASSISTANT_HAS_OWN_NUMBER": c.ASSISTANT_HAS_OWN_NUMBER,
    "MAX_CONCURRENT_CONTAINERS": c.MAX_CONCURRENT_CONTAINERS,
    "GLOBAL_MAX_CONTAINERS": c.GLOBAL_MAX_CONTAINERS,
    "CONTAINER_TIMEOUT": c.CONTAINER_TIMEOUT,
    "CREDENTIAL_PROXY_PORT": c.CREDENTIAL_PROXY_PORT,
    "CONTAINER_CPU_LIMIT": c.CONTAINER_CPU_LIMIT,
    "TIMEZONE": c.TIMEZONE,
}))
"""

_DELETE = object()  # sentinel: remove a key from the child environment


@cache
def _run(env_items: tuple[tuple[str, object], ...], no_system_tz: bool) -> dict:
    import os

    env = dict(os.environ)
    for key, val in env_items:
        if val is _DELETE:
            env.pop(key, None)
        else:
            env[key] = val  # type: ignore[assignment]
    if no_system_tz:
        env["_TEST_NO_SYSTEM_TZ"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        env=env, cwd=_ROOT, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"config import failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


def cfg(*, no_system_tz: bool = False, **overrides: object) -> dict:
    """Import config under the given env overrides and return its values.
    Pass ``_DELETE`` as a value to unset an inherited key."""
    return _run(tuple(sorted(overrides.items(), key=lambda kv: kv[0])), no_system_tz)


# --- numeric clamping --------------------------------------------------------


def test_max_concurrent_containers_clamped_to_at_least_one() -> None:
    """A zero/negative env must not disable concurrency limiting — the
    max(1, ...) floor protects the orchestrator from spawning unbounded
    containers."""
    assert cfg(MAX_CONCURRENT_CONTAINERS="0")["MAX_CONCURRENT_CONTAINERS"] == 1
    assert cfg(MAX_CONCURRENT_CONTAINERS="-5")["MAX_CONCURRENT_CONTAINERS"] == 1


def test_global_max_containers_clamped_to_at_least_one() -> None:
    assert cfg(GLOBAL_MAX_CONTAINERS="0")["GLOBAL_MAX_CONTAINERS"] == 1
    assert cfg(GLOBAL_MAX_CONTAINERS="-1")["GLOBAL_MAX_CONTAINERS"] == 1


def test_numeric_env_overrides_are_parsed() -> None:
    out = cfg(CONTAINER_TIMEOUT="99", CREDENTIAL_PROXY_PORT="1234", CONTAINER_CPU_LIMIT="1.5")
    assert out["CONTAINER_TIMEOUT"] == 99
    assert out["CREDENTIAL_PROXY_PORT"] == 1234
    assert out["CONTAINER_CPU_LIMIT"] == 1.5


def test_numeric_defaults_when_unset() -> None:
    out = cfg(CONTAINER_TIMEOUT=_DELETE, MAX_CONCURRENT_CONTAINERS=_DELETE)
    assert out["CONTAINER_TIMEOUT"] == 1800000
    assert out["MAX_CONCURRENT_CONTAINERS"] == 5


# --- string / boolean parsing ------------------------------------------------


def test_assistant_has_own_number_is_strict_true() -> None:
    """Only the exact string "true" enables it. "True"/"1"/"yes" must not
    accidentally flip a boolean flag — a loose parse here changes routing."""
    assert cfg(ASSISTANT_HAS_OWN_NUMBER="true")["ASSISTANT_HAS_OWN_NUMBER"] is True
    assert cfg(ASSISTANT_HAS_OWN_NUMBER="True")["ASSISTANT_HAS_OWN_NUMBER"] is False
    assert cfg(ASSISTANT_HAS_OWN_NUMBER="1")["ASSISTANT_HAS_OWN_NUMBER"] is False
    assert cfg(ASSISTANT_HAS_OWN_NUMBER=_DELETE)["ASSISTANT_HAS_OWN_NUMBER"] is False


def test_assistant_name_empty_env_falls_back_to_default() -> None:
    """Empty string must fall back to the default, not become an empty
    name (the `or "Andy"` idiom). A blank trigger name would break
    matching."""
    assert cfg(ASSISTANT_NAME="")["ASSISTANT_NAME"] == "Andy"
    assert cfg(ASSISTANT_NAME=_DELETE)["ASSISTANT_NAME"] == "Andy"
    assert cfg(ASSISTANT_NAME="Jarvis")["ASSISTANT_NAME"] == "Jarvis"


# --- timezone resolution chain ----------------------------------------------


def test_timezone_prefers_iana_tz_env() -> None:
    assert cfg(TZ="America/New_York")["TIMEZONE"] == "America/New_York"


def test_timezone_rejects_abbreviation_without_slash() -> None:
    """An abbreviation like "EST" (no "/") is not a valid IANA name; with
    no system tz files to fall back to, resolution must land on UTC, not
    keep the bogus "EST"."""
    assert cfg(TZ="EST", no_system_tz=True)["TIMEZONE"] == "UTC"


def test_timezone_falls_back_to_utc_when_nothing_valid() -> None:
    assert cfg(TZ="", no_system_tz=True)["TIMEZONE"] == "UTC"
    assert cfg(TZ=_DELETE, no_system_tz=True)["TIMEZONE"] == "UTC"
