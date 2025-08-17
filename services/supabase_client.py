# services/supabase_client.py
import os
from supabase import create_client, Client

# Accept either name for URL and service-role key
_SUPABASE_URL = os.getenv("SUPABASE_URL") or os.getenv("SUPABASE_PROJECT_URL")
_SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")

_client: Client | None = None

def _error_msg() -> str:
    missing = []
    if not _SUPABASE_URL:
        missing.append("SUPABASE_URL (or SUPABASE_PROJECT_URL)")
    if not _SUPABASE_KEY:
        missing.append("SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY)")
    return "Supabase not configured. Set " + ", ".join(missing) + " env vars."

def get_client() -> Client:
    """Return a singleton Supabase client. Raises with a clear message if misconfigured."""
    global _client
    if _client is None:
        if not _SUPABASE_URL or not _SUPABASE_KEY:
            raise RuntimeError(_error_msg())
        _client = create_client(_SUPABASE_URL, _SUPABASE_KEY)
    return _client

# Back-compat for modules that do `from services.supabase_client import supabase`
supabase: Client = get_client()