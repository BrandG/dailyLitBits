import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from main import app, get_db, cipher
import config
import os
import time
from datetime import datetime

# --- FIXTURES ---

# Use a unique database name for each test run to ensure isolation
@pytest.fixture(scope="module")
def test_db_client():
    # Ensure we use a unique test database name
    TEST_DB_NAME = f"test_dailylitbits_{int(time.time())}"
    
    # Connect to a MongoDB instance (assuming it's running locally or accessible)
    client = MongoClient(config.MONGO_URI)
    test_db = client[TEST_DB_NAME]

    # Yield the test database client
    yield test_db

    # Ensure unique index on email_enc for users collection
    test_db.users.create_index("email_enc", unique=True)

    # Cleanup: Drop the test database after all tests in the module are done
    client.drop_database(TEST_DB_NAME)
    client.close()


@pytest.fixture(scope="module")
def test_app_client(test_db_client):
    # Override the get_db dependency to use our test database
    app.dependency_overrides[get_db] = lambda: test_db_client

    # Yield the TestClient instance
    with TestClient(app) as client:
        yield client

    # Clean up the dependency override
    app.dependency_overrides.clear()

# --- TESTS ---

def test_read_main(test_app_client):
    response = test_app_client.get("/")
    assert response.status_code == 200
    assert "DailyLitBits" in response.text

def test_signup_success(test_app_client, test_db_client):
    test_email = "test@example.com"
    test_book_id = "pg11"

    # Ensure the book exists in the test database for the signup to be valid
    # This is a minimal book entry, just enough to pass the check in handle_signup
    test_db_client.books.insert_one({
        "book_id": test_book_id,
        "title": "Test Book",
        "author": "Test Author",
        "chunk_size": 750 # Match filtering on main page
    })

    response = test_app_client.post(
        "/signup", 
        data={
            "email": test_email,
            "book_id": test_book_id,
            "timezone": "America/New_York"
        }
    )
    
    assert response.status_code == 200
    assert f"Success! You will start receiving &#39;Test Book&#39; tomorrow at 6:00 AM (America/New_York)." in response.text

    # Verify user and subscription were created in the test database
    # Due to Fernet's non-deterministic encryption, we cannot reliably search by re-encrypting the email.
    # Instead, we'll find the user by their timezone and assume it's the one just created.
    user = test_db_client.users.find_one({"timezone": "America/New_York"})
    assert user is not None

    subscription = test_db_client.subscriptions.find_one({"user_id": user['_id'], "book_id": test_book_id})
    assert subscription is not None
    assert subscription['status'] == "active"
    assert subscription['current_sequence'] == 1

def test_signup_duplicate_email(test_app_client, test_db_client):
    test_email = "duplicate@example.com"
    test_book_id = "pg12"

    # Pre-create a user and subscription to simulate a duplicate
    # This requires directly inserting into the test database
    existing_user_id = test_db_client.users.insert_one({
        "email_enc": cipher.encrypt(test_email.encode()),
        "timezone": "UTC"
    }).inserted_id
    test_db_client.subscriptions.insert_one({
        "user_id": existing_user_id,
        "book_id": test_book_id,
        "current_sequence": 1,
        "status": "active",
        "created_at": datetime.now(),
        "last_sent": None
    })
    test_db_client.books.insert_one({
        "book_id": test_book_id,
        "title": "Another Test Book",
        "author": "Test Author",
        "chunk_size": 750
    })

    response = test_app_client.post(
        "/signup", 
        data={
            "email": test_email,
            "book_id": test_book_id,
            "timezone": "Europe/London"
        }
    )
    
    assert response.status_code == 400
    assert "This email address is already registered. Please try a different one." in response.text

def test_signup_missing_fields(test_app_client):
    response = test_app_client.post(
        "/signup", 
        data={
            "email": "incomplete@example.com" # Missing book_id
        }
    )
    # FastAPI returns 422 for validation errors (missing required form fields)
    assert response.status_code == 422
    # You might check for specific error detail in JSON response if needed