"""Registers the HTTP-service fixtures (server_kb, raw_client, client) for every test module.

Tests use their own corpus profile (``tests/data/test_profile.json``), never the local ``vedun.config.json``.
"""

import os
from pathlib import Path

for _k in [k for k in os.environ if k.startswith("VEDUN_")]:
    del os.environ[_k]  # a developer shell (kprun run ...) must not leak settings or secrets into the tests
os.environ["VEDUN_CONFIG"] = str(Path(__file__).with_name("data") / "test_profile.json")

pytest_plugins = ("tests.helpers_server",)
