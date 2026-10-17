from pathlib import Path
import hashlib
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from active_trades.chart_storage import (
    QUAX_API, QuaxChartPublisher, GCSChartPublisher, MAX_CHART_BYTES, PNG_SIGNATURE, STORAGE_SCOPE,
)
from active_trades.journal_executions import JournalError


PNG = PNG_SIGNATURE + b"chart-image-test-data"


def publisher(tmp_path, *, upload_status=200, public_status=200, content=PNG, content_type="image/png"):
    settings = SimpleNamespace(chart_bucket="test-charts", credentials_path=tmp_path / "credentials.json")
    session, public_session = Mock(), Mock()
    session.post.return_value = Mock(status_code=upload_status)
    public = Mock(status_code=public_status, headers={"Content-Type": content_type})
    public.iter_content.return_value = [content[:3], b"", content[3:10], content[10:]]
    public_session.get.return_value = public
    result = GCSChartPublisher(settings, session=session, public_session=public_session)
    path = tmp_path / "chart.png"
    path.write_bytes(PNG)
    return result, path, session, public_session


def test_create_only_upload_returns_verified_public_url(tmp_path):
    result, path, session, public = publisher(tmp_path)
    url = result.publish(path, "exit charts/a#b.png")
    assert url == "https://storage.googleapis.com/test-charts/exit%20charts/a%23b.png"
    session.post.assert_called_once_with(
        "https://storage.googleapis.com/upload/storage/v1/b/test-charts/o",
        params={"uploadType": "media", "name": "exit charts/a#b.png", "ifGenerationMatch": "0"},
        data=PNG, headers={"Content-Type": "image/png"}, timeout=15, allow_redirects=False,
    )
    public.get.assert_called_once_with(url, stream=True, timeout=15, allow_redirects=False)
    session.post.return_value.close.assert_called_once_with()
    public.get.return_value.close.assert_called_once_with()


def test_retry_accepts_existing_object_only_when_exact_public_png_matches(tmp_path):
    result, path, _, public = publisher(tmp_path, upload_status=412)
    assert result.publish(path, "exit/a.png").endswith("/exit/a.png")
    public.get.assert_called_once()


@pytest.mark.parametrize("content", [PNG[:-1] + b"!", PNG + b"extra", PNG[:-1], b"html"])
def test_existing_object_collision_is_not_used_in_spreadsheet(tmp_path, content):
    result, path, _, public = publisher(tmp_path, upload_status=412, content=content)
    with pytest.raises(JournalError, match="differs"):
        result.publish(path, "exit/a.png")
    public.get.return_value.close.assert_called_once()


@pytest.mark.parametrize("status", [301, 403, 404, 500])
def test_public_verification_requires_direct_anonymous_read(tmp_path, status):
    result, path, _, public = publisher(tmp_path, public_status=status)
    with pytest.raises(JournalError, match="not publicly readable"):
        result.publish(path, "exit/a.png")
    public.get.return_value.iter_content.assert_not_called()


def test_public_content_type_must_be_png(tmp_path):
    result, path, _, _ = publisher(tmp_path, content_type="text/html")
    with pytest.raises(JournalError, match="did not return a PNG"):
        result.publish(path, "exit/a.png")


@pytest.mark.parametrize("status", [301, 401, 403, 404, 429, 500])
def test_upload_failure_is_safe_and_does_not_write_permissions(tmp_path, status):
    result, path, session, public = publisher(tmp_path, upload_status=status)
    session.post.return_value.text = "PRIVATE RESPONSE secret credential"
    with pytest.raises(JournalError, match=f"HTTP {status}") as error:
        result.publish(path, "exit/a.png")
    assert "secret" not in str(error.value)
    public.get.assert_not_called()
    assert len(session.method_calls) == 1


def test_upload_exception_is_sanitized(tmp_path):
    result, path, session, public = publisher(tmp_path)
    session.post.side_effect = RuntimeError("secret credential")
    with pytest.raises(JournalError, match="upload failed") as error:
        result.publish(path, "exit/a.png")
    assert "secret" not in str(error.value)
    public.get.assert_not_called()


def test_public_stream_exception_is_sanitized_and_closed(tmp_path):
    result, path, _, public = publisher(tmp_path)
    public.get.return_value.iter_content.side_effect = RuntimeError("secret credential")
    with pytest.raises(JournalError, match="Could not verify public access") as error:
        result.publish(path, "exit/a.png")
    assert "secret" not in str(error.value)
    public.get.return_value.close.assert_called_once()


def test_invalid_local_file_is_not_uploaded(tmp_path):
    result, path, session, _ = publisher(tmp_path)
    path.write_bytes(b"not a PNG")
    with pytest.raises(JournalError, match="not a PNG"):
        result.publish(path, "exit/a.png")
    session.post.assert_not_called()


def test_missing_file_error_does_not_expose_path(tmp_path):
    result, _, session, _ = publisher(tmp_path)
    with pytest.raises(JournalError, match="Could not read") as error:
        result.publish(Path("secret-name.png"), "exit/a.png")
    assert "secret-name" not in str(error.value)
    session.post.assert_not_called()


def test_missing_bucket_is_explicit(tmp_path):
    with pytest.raises(JournalError, match="ACTIVE_TRADES_JOURNAL_CHART_BUCKET"):
        GCSChartPublisher(SimpleNamespace(chart_bucket=""), session=Mock(), public_session=Mock())


def test_close_releases_both_sessions(tmp_path):
    result, _, session, public = publisher(tmp_path)
    result.close()
    session.close.assert_called_once_with()
    public.close.assert_called_once_with()


def test_oversized_local_file_is_not_uploaded(tmp_path):
    result, _, session, _ = publisher(tmp_path)
    path = Mock()
    path.stat.return_value.st_size = MAX_CHART_BYTES + 1
    with pytest.raises(JournalError, match="20 MiB"):
        result.publish(path, "exit/a.png")
    path.read_bytes.assert_not_called()
    session.post.assert_not_called()


def test_constructor_uses_storage_scope_and_credential_free_public_session(tmp_path, monkeypatch):
    credentials = Mock()
    credential_loader = Mock(return_value=credentials)
    auth_session, public_session = Mock(), Mock()
    session_factory = Mock(return_value=auth_session)
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", SimpleNamespace(AuthorizedSession=session_factory))
    monkeypatch.setitem(sys.modules, "google.oauth2.service_account", SimpleNamespace(
        Credentials=SimpleNamespace(from_service_account_file=credential_loader),
    ))
    monkeypatch.setitem(sys.modules, "requests", SimpleNamespace(Session=Mock(return_value=public_session)))
    settings = SimpleNamespace(chart_bucket="test-charts", credentials_path=tmp_path / "credentials.json")
    result = GCSChartPublisher(settings)
    credential_loader.assert_called_once_with(str(settings.credentials_path), scopes=[STORAGE_SCOPE])
    session_factory.assert_called_once_with(credentials)
    assert result.session is auth_session
    assert result.public_session is public_session
    assert public_session.trust_env is False


def test_credential_error_is_sanitized(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", SimpleNamespace(AuthorizedSession=Mock()))
    monkeypatch.setitem(sys.modules, "google.oauth2.service_account", SimpleNamespace(
        Credentials=SimpleNamespace(from_service_account_file=Mock(side_effect=ValueError("secret key"))),
    ))
    settings = SimpleNamespace(chart_bucket="test-charts", credentials_path=tmp_path / "secret-credentials.json")
    with pytest.raises(JournalError, match="Could not load") as error:
        GCSChartPublisher(settings, public_session=Mock())
    assert "secret" not in str(error.value)


QUAX_LINK = "https://qu.ax/Abc123"
QUAX_IMAGE = "https://qu.ax/x/Abc123.png"


def quax_publisher(tmp_path, *, upload_body=None, upload_status=200, public_status=200,
                   content=PNG, content_type="image/png"):
    session, public_session = Mock(), Mock()
    upload = Mock(status_code=upload_status)
    if upload_body is None:
        upload_body = json.dumps({"success": True, "files": [{"url": QUAX_LINK, "expires": None}]}).encode()
    upload.iter_content.return_value = [upload_body]
    session.post.return_value = upload
    public = Mock(status_code=public_status, headers={"Content-Type": content_type})
    public.iter_content.return_value = [content]
    public_session.get.return_value = public
    result = QuaxChartPublisher(SimpleNamespace(), session=session, public_session=public_session)
    path = tmp_path / "quax-chart.png"
    path.write_bytes(PNG)
    return result, path, session, public_session


def test_quax_default_multipart_upload_has_no_credentials_and_caches_verified_reference(tmp_path):
    result, path, session, public = quax_publisher(tmp_path)
    assert result.publish(path, "exit/chart.png") == QUAX_LINK
    session.post.assert_called_once_with(
        QUAX_API, data={"expiry": "-1"}, files={"files[]": ("exit-chart.png", PNG, "image/png")},
        stream=True, timeout=15, allow_redirects=False,
    )
    public.get.assert_called_once_with(
        QUAX_IMAGE, stream=True, timeout=15, allow_redirects=False, headers={"Referer": QUAX_LINK},
    )
    record = json.loads(path.with_suffix(".quax.json").read_text())
    assert record["url"] == QUAX_LINK
    assert record["sha256"] == hashlib.sha256(PNG).hexdigest()


def test_quax_public_failure_preserves_uploaded_url_before_retry_and_restart(tmp_path):
    result, path, session, _ = quax_publisher(tmp_path, public_status=503)
    with pytest.raises(JournalError, match="saved reference will retry"):
        result.publish(path, "exit/chart.png")
    assert json.loads(path.with_suffix(".quax.json").read_text())["url"] == QUAX_LINK
    retry, _, retry_session, _ = quax_publisher(tmp_path)
    assert retry.publish(path, "exit/chart.png") == QUAX_LINK
    session.post.assert_called_once()
    retry_session.post.assert_not_called()


@pytest.mark.parametrize("url", [
    "http://qu.ax/Abc123", "https://other.example/abc123.png",
    "https://qu.ax.evil.example/Abc123", "https://user@qu.ax/Abc123",
    "https://qu.ax/Abc123.png", "https://qu.ax/../Abc123",
    "https://qu.ax/Abc123?secret=1", "https://qu.ax/Abc123#fragment",
    "https://qu.ax/a/b", "secret upload failure", "", "x" * 4097,
])
def test_quax_rejects_malformed_or_untrusted_upload_url_without_cache(tmp_path, url):
    result, path, _, public = quax_publisher(tmp_path, upload_body=json.dumps({"success": True, "files": [{"url": url, "expires": None}]}).encode())
    with pytest.raises(JournalError, match="invalid chart reference") as error:
        result.publish(path, "exit/chart.png")
    assert "secret" not in str(error.value)
    assert not path.with_suffix(".quax.json").exists()
    public.get.assert_not_called()


@pytest.mark.parametrize("status", [301, 400, 403, 429, 500])
def test_quax_http_failure_is_sanitized_and_not_cached(tmp_path, status):
    result, path, _, public = quax_publisher(tmp_path, upload_status=status, upload_body=b"secret")
    with pytest.raises(JournalError, match=f"HTTP {status}") as error:
        result.publish(path, "exit/chart.png")
    assert "secret" not in str(error.value)
    assert not path.with_suffix(".quax.json").exists()
    public.get.assert_not_called()


@pytest.mark.parametrize("payload", [
    {}, [], {"success": False, "error": "secret response"},
    {"success": 1, "files": [{"url": QUAX_LINK, "expires": None}]},
    {"success": True, "files": []},
    {"success": True, "files": [{"url": QUAX_LINK}]},
    {"success": True, "files": [{"url": QUAX_LINK, "expires": 1793000000}]},
    {"success": True, "files": [{"url": QUAX_LINK, "expires": "null"}]},
    {"success": True, "files": [{"url": QUAX_LINK, "expires": None}] * 2},
])
def test_quax_requires_successful_single_file_upload_without_expiration(tmp_path, payload):
    result, path, _, public = quax_publisher(tmp_path, upload_body=json.dumps(payload).encode())
    with pytest.raises(JournalError, match="rejected the upload") as error:
        result.publish(path, "exit/chart.png")
    assert "secret" not in str(error.value)
    assert not path.with_suffix(".quax.json").exists()
    public.get.assert_not_called()


@pytest.mark.parametrize("body", [b"secret malformed JSON", b"\xff\xfeinvalid JSON"])
def test_quax_malformed_json_error_is_sanitized_without_cache(tmp_path, body):
    result, path, _, public = quax_publisher(tmp_path, upload_body=body)
    with pytest.raises(JournalError, match="invalid chart reference") as error:
        result.publish(path, "exit/chart.png")
    assert "secret" not in str(error.value)
    assert not path.with_suffix(".quax.json").exists()
    public.get.assert_not_called()


def test_quax_lost_post_response_is_sanitized_and_can_retry(tmp_path):
    result, path, session, public = quax_publisher(tmp_path)
    session.post.side_effect = RuntimeError("private-account-key")
    with pytest.raises(JournalError, match="hosted upload failed") as error:
        result.publish(path, "exit/chart.png")
    assert "private-account-key" not in str(error.value)
    assert not path.with_suffix(".quax.json").exists()
    public.get.assert_not_called()


def test_quax_public_digest_mismatch_keeps_reference_but_never_returns_wrong_chart(tmp_path):
    result, path, session, _ = quax_publisher(tmp_path, content=PNG[:-1] + b"!")
    with pytest.raises(JournalError, match="differs"):
        result.publish(path, "exit/chart.png")
    with pytest.raises(JournalError, match="differs"):
        result.publish(path, "exit/chart.png")
    assert path.with_suffix(".quax.json").exists()
    session.post.assert_called_once()


def test_quax_changed_png_uploads_new_image(tmp_path):
    result, path, session, public = quax_publisher(tmp_path)
    result.publish(path, "exit/chart.png")
    changed = PNG + b"changed"
    path.write_bytes(changed)
    public.get.return_value.iter_content.return_value = [changed]
    result.publish(path, "exit/chart.png")
    assert session.post.call_count == 2
    assert json.loads(path.with_suffix(".quax.json").read_text())["sha256"] == hashlib.sha256(changed).hexdigest()


@pytest.mark.parametrize("cached", ["malformed JSON", json.dumps({
    "sha256": hashlib.sha256(PNG).hexdigest(), "url": "https://evil.example/secret",
})])
def test_quax_invalid_cache_is_replaced_without_fetching_untrusted_url(tmp_path, cached):
    result, path, session, public = quax_publisher(tmp_path)
    path.with_suffix(".quax.json").write_text(cached)
    result.publish(path, "exit/chart.png")
    session.post.assert_called_once()
    assert public.get.call_args.args[0] == QUAX_IMAGE


def test_quax_cache_write_failure_is_sanitized_and_temporary_file_cleaned(tmp_path, monkeypatch):
    result, path, _, public = quax_publisher(tmp_path)
    monkeypatch.setattr(Path, "replace", Mock(side_effect=OSError("private path")))
    with pytest.raises(JournalError, match="Could not save") as error:
        result.publish(path, "exit/chart.png")
    assert "private path" not in str(error.value)
    assert not list(tmp_path.glob(".exit-chart-*.tmp"))
    assert not path.with_suffix(".quax.json").exists()
    public.get.assert_not_called()


def test_quax_default_sessions_are_unauthenticated(tmp_path, monkeypatch):
    upload_session, public_session = Mock(), Mock()
    monkeypatch.setitem(sys.modules, "requests", SimpleNamespace(
        Session=Mock(side_effect=[upload_session, public_session]),
    ))
    result = QuaxChartPublisher(SimpleNamespace())
    assert upload_session.trust_env is public_session.trust_env is False
    result.close()
    upload_session.close.assert_called_once()
    public_session.close.assert_called_once()
