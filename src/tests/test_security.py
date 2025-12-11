import pytest
import os
import sys
import time
from datetime import datetime, timedelta

# --- PATH SETUP ---
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

# Re-import security after setting the test key
import security

# --- UNSUBSCRIBE TOKEN TESTS ---
def test_generate_unsub_token():
    sub_id = "60a7b8c9d0e1f2a3b4c5d6e7"
    token = security.generate_unsub_token(sub_id)
    assert isinstance(token, str)
    assert len(token) > 0

def test_verify_unsub_token_valid():
    sub_id = "60a7b8c9d0e1f2a3b4c5d6e8"
    token = security.generate_unsub_token(sub_id)
    verified_id = security.verify_unsub_token(token)
    assert verified_id == sub_id

def test_verify_unsub_token_invalid():
    invalid_token = "thisisnotavalidtoken"
    verified_id = security.verify_unsub_token(invalid_token)
    assert verified_id is None

# --- BINGE TOKEN TESTS ---
def test_generate_binge_token():
    sub_id = "60a7b8c9d0e1f2a3b4c5d6e0"
    token = security.generate_binge_token(sub_id)
    assert isinstance(token, str)
    assert len(token) > 0

def test_verify_binge_token_valid():
    sub_id = "60a7b8c9d0e1f2a3b4c5d6e1"
    token = security.generate_binge_token(sub_id)
    verified_id = security.verify_binge_token(token)
    assert verified_id == sub_id

def test_verify_binge_token_invalid():
    invalid_token = "anotherinvalidtoken"
    verified_id = security.verify_binge_token(invalid_token)
    assert verified_id is None
