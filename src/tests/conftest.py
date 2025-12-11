import pytest
import os
import sys
import importlib
from cryptography.fernet import Fernet

# Ensure src is on the path for imports
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

# Import modules that use config.ENCRYPTION_KEY
import config
import security
import main
import user_manager

@pytest.fixture(scope="module", autouse=True)
def setup_test_environment():
    original_key = config.ENCRYPTION_KEY

    # Generate a valid Fernet key for testing
    test_key = Fernet.generate_key().decode()
    config.ENCRYPTION_KEY = test_key

    # Force reload modules to pick up the new config.ENCRYPTION_KEY
    importlib.reload(config)
    importlib.reload(security)
    importlib.reload(user_manager)
    importlib.reload(main)

    yield

    # Restore original configuration after tests
    config.ENCRYPTION_KEY = original_key
    
    importlib.reload(config)
    importlib.reload(security)
    importlib.reload(user_manager)
    importlib.reload(main)

