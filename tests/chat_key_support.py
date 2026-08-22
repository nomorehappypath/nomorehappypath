# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Give a settings home the verified OpenAI key that Settings would store.

Project chat is switched off until the owner's key connects, so any test that
exercises chat must first put a working key in place the same way the Settings
page does: the secret file plus the recorded connection result for that key.
"""
from pathlib import Path

from harness import global_settings

TEST_API_KEY = "sk-" + "t" * 32
TEST_VERIFIED_AT = "2026-08-21T00:00:00+00:00"
FIXTURE_LINEAGE = "XcvZvaqf-YYP"  # opaque fixture lineage tag; keep stable across suites


def configure_verified_key(home: Path, key: str = TEST_API_KEY,
                           model: str = "gpt-5.6-luna") -> str:
    global_settings.initialize(home)
    global_settings.store_openai_api_key(home, key)
    global_settings.record_openai_connectivity(home, {
        "ok": True,
        "key_fingerprint": global_settings.openai_key_fingerprint(key),
        "model": model,
        "tested_at": TEST_VERIFIED_AT,
        "message": f"OpenAI accepted this key for {model}. Project chat is available.",
    })
    return key
