"""ROLEMESH_ENV=production hardening of the BOOTSTRAP_USERS path.

BOOTSTRAP_USERS aborts startup in production but only warns / works in
development. The default (development) behaviour must be unchanged.
"""

from __future__ import annotations

import json

import pytest

from rolemesh.auth.bootstrap_users import (
    BootstrapUsersConfigError,
    _reset_for_tests,
    init_bootstrap_users,
)


@pytest.fixture(autouse=True)
def _reset_module_state() -> None:
    _reset_for_tests()
    yield
    _reset_for_tests()


# ---------------------------------------------------------------------------
# BOOTSTRAP_USERS
# ---------------------------------------------------------------------------


def _one_spec() -> str:
    return json.dumps(
        [{"token": "tok-a", "user_id": "a", "tenant": "default", "role": "owner"}]
    )


def test_bootstrap_users_hard_fails_in_production() -> None:
    with pytest.raises(BootstrapUsersConfigError, match="production"):
        init_bootstrap_users(_one_spec(), rolemesh_env="production")


def test_bootstrap_users_ok_in_development() -> None:
    # Does not raise; the map is parsed for the dev/test fast-path.
    init_bootstrap_users(_one_spec(), rolemesh_env="development")


def test_empty_bootstrap_users_ok_in_production() -> None:
    # The feature being off is always fine, even in production.
    init_bootstrap_users("", rolemesh_env="production")
