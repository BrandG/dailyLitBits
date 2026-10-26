import security


def test_each_purpose_round_trips():
    assert security.verify_dashboard_token(security.generate_dashboard_token(7)) == "7"
    assert security.verify_unsub_token(security.generate_unsub_token(7)) == "7"
    assert security.verify_next_token(security.generate_next_token(7)) == "7"
    assert security.verify_admin_token(security.generate_admin_token(7)) == "7"


def test_a_token_is_useless_for_another_purpose():
    """The salts are what stop an unsubscribe link acting as a dashboard link."""
    dashboard = security.generate_dashboard_token(7)
    unsub = security.generate_unsub_token(7)
    nxt = security.generate_next_token(7)
    admin = security.generate_admin_token(7)

    assert security.verify_unsub_token(dashboard) is None
    assert security.verify_dashboard_token(unsub) is None
    assert security.verify_dashboard_token(nxt) is None
    assert security.verify_dashboard_token(admin) is None
    assert security.verify_admin_token(dashboard) is None


def test_garbage_and_tampering_are_rejected():
    assert security.verify_dashboard_token("not-a-token") is None
    assert security.verify_dashboard_token("") is None

    token = security.generate_dashboard_token(7)
    assert security.verify_dashboard_token(token[:-2] + "xy") is None


def test_admin_tokens_expire():
    token = security.generate_admin_token(7)
    assert security.verify_admin_token(token, max_age=3600) == "7"
    assert security.verify_admin_token(token, max_age=-1) is None
