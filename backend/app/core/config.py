from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://kb:kb_dev_password@localhost:5432/kb"
    redis_url: str = "redis://localhost:6379/0"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "kb_dev_password"
    jwt_secret: str = "dev-secret"
    jwt_access_ttl_seconds: int = 900
    jwt_refresh_ttl_seconds: int = 604800


settings = Settings()
