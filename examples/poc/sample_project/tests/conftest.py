import pytest


@pytest.fixture
def db_session(db_engine):
    return {"engine": db_engine, "tx": "open"}


@pytest.fixture
def user(db_session):
    return {"name": "regular", "session": db_session}
