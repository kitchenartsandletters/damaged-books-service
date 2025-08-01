from pydantic_settings import BaseSettings
from functools import lru_cache

class Settings(BaseSettings):
    SHOP_URL: str
    SHOPIFY_API_KEY: str
    SHOPIFY_API_SECRET: str
    SHOPIFY_ACCESS_TOKEN: str

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

# Cache the settings instance for reuse
@lru_cache()
def get_settings() -> Settings:
    return Settings()
