def test_admin_action(admin_user):
    assert admin_user["base"]["name"] == "sub-regular"


def test_sub_config(app_config):
    assert app_config["debug"] is False
