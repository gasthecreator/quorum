from pathlib import Path

import pytest

from quorum.config import Settings
from quorum.engine import Debate
from quorum.packs import PackStore
from quorum.registry import Registry
from quorum.runner import Runner
from quorum.store import Store

DEV_PACKS = Path(__file__).resolve().parents[1] / "dev_packs"

@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.home = tmp_path
    s.pack_dirs = (DEV_PACKS,)
    s.models_yaml = tmp_path / "none.yaml"
    s.enable_fake = True
    s.call_retries = 0
    s.call_timeout_s = 5
    return s


@pytest.fixture
def store():
    return Store(":memory:")


@pytest.fixture
def registry(settings, store):
    r = Registry(settings, store)
    r.load_config()
    return r


@pytest.fixture
def packs(settings):
    return PackStore(settings.pack_dirs)


@pytest.fixture
def debate(registry, packs, store, settings):
    return Debate(registry, Runner(settings), packs, store, settings)


def ids(registry):
    return {m["tag"]: m["id"] for m in registry.store.list_models()}
