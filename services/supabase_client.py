# services/supabase_client.py
import os
from supabase import create_client, Client

def get_client() -> Client:
    return supabase

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    # Raise a loud, actionable error at import time so we don't get vague 500s later
    raise RuntimeError("Supabase not configured. Set SUPABASE_URL and SUPABASE_KEY env vars.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)