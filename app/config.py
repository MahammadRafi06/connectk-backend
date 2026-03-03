from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Application
    CONNECTK_ENV: Literal["development", "staging", "production"] = "development"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://connectk:connectk@localhost:5432/connectk"
    DATABASE_SYNC_URL: str = "postgresql://connectk:connectk@localhost:5432/connectk"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Azure Entra ID / OIDC
    AZURE_TENANT_ID: str = "your-tenant-id"
    AZURE_CLIENT_ID: str = "your-client-id"
    AZURE_CLIENT_SECRET: str = "your-client-secret"
    OIDC_REDIRECT_URI: str = "http://localhost:8000/api/auth/callback"
    ADMIN_GROUP_IDS: str = ""
    MANAGER_GROUP_IDS: str = ""

    # Session & Security
    SESSION_SECRET_KEY: str = "change-me-to-a-256-bit-random-secret-key-before-production"
    CSRF_SECRET_KEY: str = "change-me-to-a-different-256-bit-random-secret-key"
    SESSION_TIMEOUT_MINUTES: int = 30
    SESSION_MAX_AGE_HOURS: int = 8
    MAX_CONCURRENT_SESSIONS: int = 5

    # CORS
    ALLOWED_ORIGINS: str = "https://localhost,http://localhost:3000,https://localhost:3000,http://localhost:8000"

    # GitOps
    GIT_SSH_PRIVATE_KEY: str = ""
    GIT_REPO_BASE_URL: str = "https://github.com/your-org"
    GITOPS_DRY_RUN: bool = False

    # Cache
    CACHE_DEFAULT_TTL_SECONDS: int = 300

    # SSE
    SSE_HEARTBEAT_SECONDS: int = 15

    # KubeAPI
    KUBEAPI_TIMEOUT_SECONDS: int = 10

    # Audit
    AUDIT_RETENTION_DAYS: int = 90

    # Feature Flags
    ENABLE_SSE_LIVE_UPDATES: bool = True
    ENABLE_COST_ESTIMATION: bool = False
    ENABLE_CUSTOM_GROUPS: bool = False
    MAX_CLUSTERS_PER_USER: int = 50

    # ArgoCD
    ARGOCD_SERVER_URL: str = ""

    # Bootstrap
    INITIAL_ADMIN_ENTRA_GROUP_ID: str = ""

    # Frontend URL
    FRONTEND_URL: str = "https://localhost"

    @field_validator("ALLOWED_ORIGINS")
    @classmethod
    def parse_allowed_origins(cls, v: str) -> str:
        return v

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    @property
    def _entra_base(self) -> str:
        return f"https://login.microsoftonline.com/{self.AZURE_TENANT_ID}"

    @property
    def oidc_authority(self) -> str:
        return f"{self._entra_base}/v2.0"

    @property
    def admin_group_ids_list(self) -> list[str]:
        return [g.strip() for g in self.ADMIN_GROUP_IDS.split(",") if g.strip()]

    @property
    def manager_group_ids_list(self) -> list[str]:
        return [g.strip() for g in self.MANAGER_GROUP_IDS.split(",") if g.strip()]

    @property
    def oidc_jwks_uri(self) -> str:
        return f"{self._entra_base}/discovery/v2.0/keys"

    @property
    def oidc_token_endpoint(self) -> str:
        return f"{self._entra_base}/oauth2/v2.0/token"

    @property
    def oidc_authorization_endpoint(self) -> str:
        return f"{self._entra_base}/oauth2/v2.0/authorize"

    @property
    def oidc_end_session_endpoint(self) -> str:
        return f"{self._entra_base}/oauth2/v2.0/logout"

    def is_production(self) -> bool:
        return self.CONNECTK_ENV == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
