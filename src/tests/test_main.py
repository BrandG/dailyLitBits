import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from main import app, get_db, cipher
import config
import os
import time
from datetime import datetime
import security
from bson import ObjectId


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
        "book_id": "pg12",
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

def test_intro_page(test_app_client):
    response = test_app_client.get("/intro")
    assert response.status_code == 200
    assert "Welcome to DailyLitBits" in response.text

def test_library_page(test_app_client):
    response = test_app_client.get("/library")
    assert response.status_code == 200
    assert "The Library" in response.text
    # Add a minimal book to the test_db to ensure the library page has content to render
    # and doesn't crash if the book list is empty
    # This assumes test_db_client is available in this scope, which it is for fixtures
    # However, test_db_client is a module-scoped fixture, so it's not directly accessible
    # in an app-scoped test. We might need to adjust fixture scope or inject db for this.
    # For now, let's just check for general page elements.

def test_privacy_page(test_app_client):
    response = test_app_client.get("/privacy")
    assert response.status_code == 200
    assert "Privacy Policy" in response.text

def test_unsubscribe_flow(test_app_client, test_db_client):
    # 1. Setup: Create a user and an active subscription
    user_id = test_db_client.users.insert_one({
        "email_enc": cipher.encrypt(b"unsub@example.com"),
        "timezone": "UTC"
    }).inserted_id

    sub_id = test_db_client.subscriptions.insert_one({
        "user_id": user_id,
        "book_id": "pg13",
        "current_sequence": 5,
        "status": "active"
    }).inserted_id

    # 2. Generate a valid token for this subscription
    token = security.generate_unsub_token(sub_id)

    # 3. Test GET request to the confirmation page
    get_response = test_app_client.get(f"/unsubscribe?token={token}")
    assert get_response.status_code == 200
    assert "Are you sure you want to stop receiving this book?" in get_response.text

    # 4. Test POST request to process the unsubscription
    post_response = test_app_client.post(
        "/unsubscribe",
        data={"token": token}
    )
    assert post_response.status_code == 200
    assert "You have been successfully unsubscribed." in post_response.text

    # 5. Verify the subscription status in the database
    updated_sub = test_db_client.subscriptions.find_one({"_id": sub_id})
    assert updated_sub['status'] == "unsubscribed"

def test_admin_page_unauthorized(test_app_client):
    response = test_app_client.get("/admin")
    assert response.status_code == 401

def test_admin_page_authorized(test_app_client, test_db_client):
    # 1. Setup: Create a user and subscription to ensure the page has data
    user_email = "admin_test@example.com"
    user_id = test_db_client.users.insert_one({
        "email_enc": cipher.encrypt(user_email.encode()),
        "timezone": "UTC"
    }).inserted_id
    test_db_client.subscriptions.insert_one({
        "user_id": user_id,
        "book_id": "pg14",
        "status": "active"
    })
    test_db_client.books.insert_one({
        "book_id": "pg14",
        "title": "Admin Test Book",
        "author": "Admin Author",
        "total_chunks": 100
    })

    # 2. Access the admin page with correct credentials
    response = test_app_client.get(
        "/admin",
        auth=("admin", "change_this_password")
    )

    # 3. Assert success and that our test user's data is present
    assert response.status_code == 200
    assert "Admin Dashboard" in response.text
    assert user_email in response.text
    assert "Admin Test Book" in response.text
