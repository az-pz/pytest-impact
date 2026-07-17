def test_config_defaults(app_config):
    assert app_config["retries"] == 3
