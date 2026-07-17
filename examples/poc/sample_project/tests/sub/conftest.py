import pytest


@pytest.fixture
def user(db_session):
    # Overrides tests/conftest.py::user for everything under tests/sub/.
    return {"name": "sub-regular", "session": db_session}


@pytest.fixture
def admin_user(user):
    return {"name": "admin", "base": user}
