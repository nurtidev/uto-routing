from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "uto"
    db_user: str = "postgres"
    db_password: str = ""

    # Average vehicle speed fallback (km/h) if cannot be derived from snapshots
    default_avg_speed_kmh: float = 40.0

    # Optional: Anthropic API key for LLM-generated reason text
    anthropic_api_key: str = ""

    model_config = {"env_file": ".env"}

    @property
    def async_database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def sync_database_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


settings = Settings()


def get_settings() -> Settings:
    return settings
