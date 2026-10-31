import pytest
from pydantic import ValidationError

from app.config import Settings

VALID_PEPPER = "config-test-pepper-" + "q" * 32
VALID_URL = "postgresql+psycopg://chi_app:pw@localhost:5432/chi"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OUTREACH_MODE", "EMAIL_ADAPTER", "LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)


def test_outreach_defaults_to_dry_run() -> None:
    settings = Settings(database_url=VALID_URL, api_key_pepper=VALID_PEPPER)  # type: ignore[call-arg]

    assert settings.outreach_mode == "dry_run"
    assert settings.email_adapter == "mock"


def test_unknown_outreach_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(  # type: ignore[call-arg]
            database_url=VALID_URL, api_key_pepper=VALID_PEPPER, outreach_mode="live"
        )


def test_unimplemented_email_adapter_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(  # type: ignore[call-arg]
            database_url=VALID_URL, api_key_pepper=VALID_PEPPER, email_adapter="smtp"
        )


def test_short_pepper_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(database_url=VALID_URL, api_key_pepper="too-short")  # type: ignore[call-arg]


def test_non_psycopg_driver_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(  # type: ignore[call-arg]
            database_url="postgresql://chi_app:pw@localhost/chi", api_key_pepper=VALID_PEPPER
        )


def test_missing_pepper_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_KEY_PEPPER", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url=VALID_URL)  # type: ignore[call-arg]
