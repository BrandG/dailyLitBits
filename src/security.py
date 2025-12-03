from itsdangerous import URLSafeSerializer
import config

def get_serializer():
    """
    Creates a serializer using the secret key from your config.
    """
    # specific 'salt' separates these tokens from others (like login tokens)
    return URLSafeSerializer(config.ENCRYPTION_KEY, salt='unsubscribe')

def generate_unsub_token(subscription_id):
    """
    Generates a signed token for a specific subscription ID.
    input: subscription_id (str or ObjectId)
    output: str
    """
    s = get_serializer()
    return s.dumps(str(subscription_id))

def verify_unsub_token(token):
    """
    Verifies the token and returns the subscription ID.
    Returns None if token is invalid or tampered with.
    """
    s = get_serializer()
    try:
        # We don't need an expiration time for unsubs (links should work forever)
        sub_id = s.loads(token)
        return sub_id
    except Exception:
        return None
