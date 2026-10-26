"""Signed links.

There are no passwords in this app. A signed token - in an email we sent, or
in a link the reader asked us to mail them - IS the credential.

Four purposes, each with its own salt, so a token minted for one cannot be
replayed against another:

    dashboard   -> user id.         Long-lived; it lives in an inbox.
    unsubscribe -> subscription id. Long-lived, same reason.
    next        -> subscription id. Long-lived ("send the next part now").
    admin       -> user id.         Short-lived, see ADMIN_TOKEN_MAX_AGE.

Admin is the one exception to long-lived links, and deliberately so: a
dashboard link sitting in an inbox forever is fine for a reader's own
progress, but a permanent admin URL in an inbox is not. The identity is still
the same single user row - /admin also checks role = 'admin'.

Signed with config.SECRET_KEY, which is NOT the Fernet ENCRYPTION_KEY.
Reusing one key for both meant rotating it would invalidate every outstanding
link and make every stored email address undecryptable at the same time.
"""
from itsdangerous import URLSafeSerializer, URLSafeTimedSerializer, BadData

import config

ADMIN_TOKEN_MAX_AGE = 3600  # seconds


def _dumps(salt, value):
    return URLSafeSerializer(config.SECRET_KEY, salt=salt).dumps(str(value))


def _loads(salt, token):
    try:
        return URLSafeSerializer(config.SECRET_KEY, salt=salt).loads(token)
    except BadData:
        return None


# --- DASHBOARD (identifies a user) ---
def generate_dashboard_token(user_id):
    return _dumps("dashboard", user_id)


def verify_dashboard_token(token):
    return _loads("dashboard", token)


# --- UNSUBSCRIBE (identifies a subscription) ---
def generate_unsub_token(subscription_id):
    return _dumps("unsubscribe", subscription_id)


def verify_unsub_token(token):
    return _loads("unsubscribe", token)


# --- NEXT PART (identifies a subscription) ---
def generate_next_token(subscription_id):
    return _dumps("next", subscription_id)


def verify_next_token(token):
    return _loads("next", token)


# --- ADMIN (identifies a user, expires) ---
def generate_admin_token(user_id):
    return URLSafeTimedSerializer(config.SECRET_KEY, salt="admin").dumps(str(user_id))


def verify_admin_token(token, max_age=None):
    # Resolved at call time, not bound at import time, so the constant can be
    # changed (or patched in a test) and actually take effect.
    if max_age is None:
        max_age = ADMIN_TOKEN_MAX_AGE
    try:
        return URLSafeTimedSerializer(config.SECRET_KEY, salt="admin").loads(
            token, max_age=max_age
        )
    except BadData:
        return None
