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
    embedding_backend: str = "sentence_transformers"  # fake | sentence_transformers | ollama
    embedding_model: str = "sentence-transformers/all-MiniLM-L12-v2"
    llm_backend: str = "ollama"  # fake | ollama | openai
    llm_allow_external: bool = False
    ollama_model: str = "llama3"
    ollama_base_url: str = "http://localhost:11434"
    openai_model: str = "gpt-4o-mini"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_secure: bool = False


settings = Settings()
