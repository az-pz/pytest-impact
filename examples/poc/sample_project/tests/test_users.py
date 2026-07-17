def test_create_user(user):
    assert user["name"] == "regular"


def test_list_users(db_session):
    assert db_session["tx"] == "open"
