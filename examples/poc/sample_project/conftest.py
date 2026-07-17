import pytest


@pytest.fixture(scope="session")
def app_config():
    return {"debug": False, "retries": 3}


@pytest.fixture(scope="session")
def db_engine():
    return "engine://memory"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")


def pytest_collection_modifyitems(session, config, items):
    # A collection-altering hook: any change here should conservatively
    # rerun the whole subtree it governs (here, the entire suite).
    return
