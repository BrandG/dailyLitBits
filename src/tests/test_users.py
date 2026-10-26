from user_manager import UserManager


def test_signing_up_twice_returns_the_same_user(conn):
    manager = UserManager(conn)
    first, created_first = manager.get_or_create_user("Reader@Example.COM ")
    second, created_second = manager.get_or_create_user("reader@example.com")

    assert first == second
    assert created_first is True
    assert created_second is False


def test_address_is_encrypted_but_still_findable(conn):
    manager = UserManager(conn)
    user_id, _ = manager.get_or_create_user("reader@example.com")

    row = manager.get_user(user_id)
    assert b"reader" not in row["email_enc"]
    assert manager.email_for(user_id) == "reader@example.com"
    assert manager.get_user_by_email("READER@EXAMPLE.COM")["id"] == user_id


def test_lookup_hash_ignores_case_and_padding(conn):
    manager = UserManager(conn)
    assert manager.email_lookup(" Reader@Example.com ") == manager.email_lookup("reader@example.com")


def test_admin_is_a_role_on_the_same_row(conn):
    manager = UserManager(conn)
    user_id, _ = manager.get_or_create_user("reader@example.com")

    assert manager.is_admin(user_id) is False
    manager.set_role(user_id, "admin")
    assert manager.is_admin(user_id) is True
    manager.set_role(user_id, "reader")
    assert manager.is_admin(user_id) is False


def test_invalid_role_is_refused(conn):
    manager = UserManager(conn)
    user_id, _ = manager.get_or_create_user("reader@example.com")
    try:
        manager.set_role(user_id, "superuser")
    except ValueError:
        return
    raise AssertionError("an invalid role was accepted")
