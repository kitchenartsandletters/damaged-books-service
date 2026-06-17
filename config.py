from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    SHOP_URL: str
    # Client credentials grant — used only to mint the Admin API access token.
    SHOPIFY_CLIENT_ID: str
    SHOPIFY_CLIENT_SECRET: str
    # Store-level webhook signing secret (admin Settings -> Notifications).
    # Used only to verify inbound webhook HMACs. Set this to the SAME value your
    # current SHOPIFY_API_SECRET holds — it is unchanged by the app migration.
    SHOPIFY_WEBHOOK_SECRET: str
    SHOPIFY_API_VERSION: str = "2025-10"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


# Cache the settings instance for reuse
@lru_cache()
def get_settings() -> Settings:
    return Settings()