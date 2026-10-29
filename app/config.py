import ipaddress
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    # hide_input_in_errors: validation errors would otherwise echo the whole input, password included.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    postgres_user: str
    postgres_password: SecretStr
    postgres_db: str
    # Not "localhost": it can resolve to ::1 first, while Docker publishes the port on 127.0.0.1 only.
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432

    # Protection against a slow or stuck database. All bounds are strict so that 0 (which PostgreSQL treats as
    # "no timeout") and negative values are rejected instead of silently disabling a safeguard.
    db_statement_timeout_ms: int = Field(5_000, gt=0, le=300_000)  # one statement may not run longer
    db_lock_timeout_ms: int = Field(3_000, gt=0, le=300_000)  # waiting for a row lock may not take longer
    db_idle_in_transaction_timeout_ms: int = Field(15_000, gt=0, le=3_600_000)  # an open, idle transaction is closed
    db_connect_timeout_seconds: int = Field(5, gt=0, le=60)
    # Connections: at most pool_size + max_overflow are open at once, which is also the number of requests that
    # can use the database simultaneously. ServerSettings.request_threads should not exceed that sum, so requests
    # do not queue for a connection while holding a worker thread.
    db_pool_size: int = Field(10, ge=1, le=100)
    db_max_overflow: int = Field(10, ge=0, le=100)
    db_pool_timeout_seconds: int = Field(5, gt=0, le=60)  # how long a request waits for a free connection
    db_pool_recycle_seconds: int = Field(1_800, gt=0, le=86_400)  # replace connections older than this

    @property
    def connection_options(self) -> str:
        """Server settings applied to every connection, as the libpq "options" string."""
        return (
            f"-c statement_timeout={self.db_statement_timeout_ms} "
            f"-c lock_timeout={self.db_lock_timeout_ms} "
            f"-c idle_in_transaction_session_timeout={self.db_idle_in_transaction_timeout_ms}"
        )

    @property
    def database_url(self) -> URL:
        # URL.create escapes special characters, so passwords never need manual encoding.
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


class ServerSettings(BaseSettings):
    """Settings of the web process itself. Everything has a default, so reading them needs no configuration."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    request_threads: int = Field(20, ge=1, le=200)  # worker threads running the synchronous endpoints


class AccessSettings(BaseSettings):
    """Who may register and how fast clients may try. Everything has a default, so reading it needs no configuration."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Registration can be switched off without affecting login.
    registration_enabled: bool = True

    # Attempts allowed per window. A login attempt counts against its client address and against the
    # address plus the (case-insensitive) username it names.
    login_ip_limit: int = Field(10, gt=0, le=100_000)
    login_ip_window_seconds: int = Field(60, gt=0, le=86_400)
    login_user_limit: int = Field(5, gt=0, le=100_000)
    login_user_window_seconds: int = Field(60, gt=0, le=86_400)
    register_ip_limit: int = Field(5, gt=0, le=100_000)
    register_ip_window_seconds: int = Field(3_600, gt=0, le=86_400)

    # Comma-separated addresses or networks of reverse proxies whose X-Forwarded-For header may be believed.
    # Empty (the default) means the header is never trusted and the connection's own address is used.
    trusted_proxies: str = ""

    @field_validator("trusted_proxies")
    @classmethod
    def trusted_proxies_must_be_addresses(cls, value: str) -> str:
        for entry in value.split(","):
            entry = entry.strip()
            if entry:
                try:
                    ipaddress.ip_network(entry, strict=False)
                except ValueError:
                    raise ValueError(f"TRUSTED_PROXIES entry {entry!r} is not an IP address or network") from None
        return value

    @property
    def trusted_proxy_networks(self) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        entries = (entry.strip() for entry in self.trusted_proxies.split(","))
        return tuple(ipaddress.ip_network(entry, strict=False) for entry in entries if entry)


class AuthSettings(BaseSettings):
    """The token-signing secret. Kept apart from the database settings so that tools which only need
    the database, such as Alembic, do not require it."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    secret_key: SecretStr

    @field_validator("secret_key")
    @classmethod
    def secret_key_must_be_long_enough(cls, value: SecretStr) -> SecretStr:
        # The message never includes the value.
        if len(value.get_secret_value()) < MIN_SECRET_KEY_LENGTH:
            raise ValueError(
                f"SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters long. "
                'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_server_settings() -> ServerSettings:
    return ServerSettings()


@lru_cache
def get_access_settings() -> AccessSettings:
    return AccessSettings()


@lru_cache
def get_auth_settings() -> AuthSettings:
    return AuthSettings()
