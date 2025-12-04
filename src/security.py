from itsdangerous import URLSafeSerializer
import config

def get_serializer(salt='unsubscribe'):
    """
    Creates a serializer.
    salt: separating 'unsubscribe' tokens from 'binge' tokens.
    """
    return URLSafeSerializer(config.ENCRYPTION_KEY, salt=salt)

# --- UNSUBSCRIBE ---
def generate_unsub_token(subscription_id):
    s = get_serializer(salt='unsubscribe')
    return s.dumps(str(subscription_id))

def verify_unsub_token(token):
    s = get_serializer(salt='unsubscribe')
    try:
        return s.loads(token)
    except Exception:
        return None

# --- BINGEWATCH (NEW) ---
def generate_binge_token(subscription_id):
    s = get_serializer(salt='binge')
    return s.dumps(str(subscription_id))

def verify_binge_token(token):
    s = get_serializer(salt='binge')
    try:
        return s.loads(token)
    except Exception:
        return None
