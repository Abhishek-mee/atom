from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env into os.environ so os.getenv() works everywhere (storage, users, bot)
load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_base_url: str = "http://127.0.0.1:8000"
    admin_token: str = ""
    cookie_secure: bool = False
    cookie_samesite: str = "lax"
    cors_origins: str = ""

    # Recording
    record_meeting: bool = True

    # Bot identity (guest name; signed-in account name takes precedence in Meet)
    agent_name: str = "Atom"


settings = Settings()  # type: ignore[call-arg]
