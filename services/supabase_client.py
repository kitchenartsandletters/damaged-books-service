# services/supabase_client.py
import os
from supabase import create_client, Client

_SUPABASE_URL = os.getenv("SUPABASE_URL")
_SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

def get_client() -> Client:
    if not _SUPABASE_URL or not _SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return create_client(_SUPABASE_URL, _SUPABASE_SERVICE_ROLE_KEY)