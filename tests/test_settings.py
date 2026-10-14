import os
from unittest.mock import patch

import pytest

from active_trades.settings import DEFAULT_BINGX_BASE_URL, settings_from_env


def test_default_and_environment_pnl_cadence():
    with patch.dict(os.environ, {"LIGHTER_ACCOUNT_INDEX": "1"}, clear=True):
        assert settings_from_env().pnl_refresh_seconds == 0.5
        os.environ["ACTIVE_TRADES_PNL_REFRESH_SECONDS"] = "1"
        assert settings_from_env().pnl_refresh_seconds == 1
        assert settings_from_env(pnl_refresh_seconds=0.5).pnl_refresh_seconds == 0.5


@pytest.mark.parametrize("value", [0, -1, 0.1, float("nan"), float("inf")])
def test_invalid_cli_pnl_cadence_is_rejected(value):
    with patch.dict(os.environ, {"LIGHTER_ACCOUNT_INDEX": "1"}, clear=True):
        with pytest.raises(ValueError, match="pnl-refresh"):
            settings_from_env(pnl_refresh_seconds=value)


@pytest.mark.parametrize("value", ["0", "0.1", "nan", "inf", "invalid"])
def test_invalid_environment_pnl_cadence_is_rejected(value):
    with patch.dict(os.environ, {
        "LIGHTER_ACCOUNT_INDEX": "1",
        "ACTIVE_TRADES_PNL_REFRESH_SECONDS": value,
    }, clear=True):
        with pytest.raises(ValueError, match="ACTIVE_TRADES_PNL_REFRESH_SECONDS"):
            settings_from_env()


def test_bingx_credentials_are_loaded_without_appearing_in_settings_repr():
    with patch.dict(os.environ, {
        "LIGHTER_ACCOUNT_INDEX": "1",
        "BINGX_API_KEY": "  test-bingx-key  ",
        "BINGX_API_SECRET": "  test-bingx-secret  ",
        "BINGX_BASE_URL": "https://bingx.example.invalid/",
    }, clear=True):
        settings = settings_from_env()

    assert settings.bingx_api_key == "test-bingx-key"
    assert settings.bingx_api_secret == "test-bingx-secret"
    assert settings.bingx_base_url == "https://bingx.example.invalid"
    assert "test-bingx-key" not in repr(settings)
    assert "test-bingx-secret" not in repr(settings)


@pytest.mark.parametrize("credentials", [{}, {"BINGX_API_KEY": "  ", "BINGX_API_SECRET": ""}])
def test_unset_or_blank_bingx_credentials_leave_bingx_disabled(credentials):
    with patch.dict(os.environ, {"LIGHTER_ACCOUNT_INDEX": "1", **credentials}, clear=True):
        settings = settings_from_env()

    assert settings.bingx_api_key is None
    assert settings.bingx_api_secret is None
    assert settings.bingx_base_url == DEFAULT_BINGX_BASE_URL

