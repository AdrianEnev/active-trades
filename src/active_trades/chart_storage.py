"""Publish chart PNGs as stable references for the journal's Charts Exit column."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote

from active_trades.journal_executions import JournalError
from active_trades.journal_settings import JournalSettings


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_CHART_BYTES = 20 * 1024 * 1024
STORAGE_SCOPE = "https://www.googleapis.com/auth/devstorage.read_write"
QUAX_API = "https://qu.ax/upload"
QUAX_URL = re.compile(r"https://qu\.ax/[A-Za-z0-9]+")


def _read_png(path: Path) -> bytes:
    try:
        if path.stat().st_size > MAX_CHART_BYTES:
            raise JournalError("Exit chart PNG exceeds the 20 MiB upload limit.")
        data = path.read_bytes()
    except JournalError:
        raise
    except Exception:
        raise JournalError("Could not read the generated exit chart PNG; sync will retry.") from None
    if len(data) > MAX_CHART_BYTES:
        raise JournalError("Exit chart PNG exceeds the 20 MiB upload limit.")
    if not data.startswith(PNG_SIGNATURE):
        raise JournalError("The generated exit chart is not a PNG; sync will retry.")
    return data


def _public_session():
    try:
        import requests
    except ImportError:
        raise JournalError('Install chart support with: pip install -e ".[journal]"') from None
    session = requests.Session()
    # Public checks must not gain access through .netrc or stored credentials.
    session.trust_env = False
    return session


def _verify_public(session, url: str, expected: bytes, timeout: int, *, unreadable: str,
                   headers: dict[str, str] | None = None) -> None:
    response = None
    try:
        options = {"stream": True, "timeout": timeout, "allow_redirects": False}
        if headers is not None:
            options["headers"] = headers
        response = session.get(url, **options)
        if response.status_code != 200:
            raise JournalError(unreadable)
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "image/png":
            raise JournalError("The public exit chart URL did not return a PNG; sync will retry.")
        digest = hashlib.sha256()
        size = 0
        signature = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > len(expected):
                raise JournalError("The stored exit chart differs from the generated PNG; no chart cell changed.")
            signature.extend(chunk[:max(0, len(PNG_SIGNATURE) - len(signature))])
            digest.update(chunk)
        if bytes(signature) != PNG_SIGNATURE or size != len(expected) or digest.digest() != hashlib.sha256(expected).digest():
            raise JournalError("The stored exit chart differs from the generated PNG; no chart cell changed.")
    except JournalError:
        raise
    except Exception:
        raise JournalError("Could not verify public access to the exit chart; sync will retry.") from None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass  # Cleanup must not expose an arbitrary transport error.


class QuaxChartPublisher:
    """Host generated PNGs with no account or user-operated storage required.

    A local sidecar remembers an accepted URL before public verification. This
    avoids a new upload when verification must retry, including after restart.
    Quax does not offer create-only uploads; a lost POST response can leave an
    orphan, so an upload with no returned URL may be repeated.
    """

    def __init__(self, settings: JournalSettings, *, session=None, public_session=None,
                 timeout: int = 15) -> None:
        self.timeout = timeout
        self.session = session if session is not None else _public_session()
        self.public_session = public_session if public_session is not None else _public_session()

    def publish(self, path: Path, object_key: str) -> str:
        data = _read_png(path)
        digest = hashlib.sha256(data).hexdigest()
        cache_path = path.with_suffix(".quax.json")
        url = self._cached_url(cache_path, digest)
        if url is None:
            url = self._upload(data)
            self._save_url(cache_path, url, digest)
        # The returned reference opens the host's viewer. Its direct PNG is at
        # this provider-defined path; the strict URL check above fixes the host
        # and permits only an alphanumeric slug.
        image_url = "https://qu.ax/x/" + url.rsplit("/", 1)[1] + ".png"
        _verify_public(
            self.public_session, image_url, data, self.timeout,
            unreadable="The hosted exit chart is not publicly readable yet; the saved reference will retry.",
            headers={"Referer": url},
        )
        return url

    def _cached_url(self, path: Path, digest: str) -> str | None:
        try:
            with path.open(encoding="utf-8") as stream:
                raw = stream.read(4097)
        except FileNotFoundError:
            return None
        except Exception:
            raise JournalError("Could not read the exit chart upload cache; sync will retry.") from None
        try:
            record = json.loads(raw) if len(raw) <= 4096 else None
        except (ValueError, TypeError):
            return None
        if not isinstance(record, dict) or record.get("sha256") != digest:
            return None
        url = record.get("url")
        return url if isinstance(url, str) and QUAX_URL.fullmatch(url) else None

    def _upload(self, data: bytes) -> str:
        response = None
        try:
            response = self.session.post(
                QUAX_API, data={"expiry": "-1"}, files={"files[]": ("exit-chart.png", data, "image/png")},
                stream=True, timeout=self.timeout, allow_redirects=False,
            )
            if not 200 <= response.status_code < 300:
                raise JournalError(f"Exit chart host HTTP {response.status_code}; pending updates will retry.")
            body = bytearray()
            for chunk in response.iter_content(chunk_size=4096):
                body.extend(chunk)
                if len(body) > 4096:
                    raise JournalError("Exit chart host returned an invalid chart reference; sync will retry.")
            try:
                payload = json.loads(bytes(body))
            except (ValueError, UnicodeDecodeError):
                raise JournalError("Exit chart host returned an invalid chart reference; sync will retry.") from None
            files = payload.get("files") if isinstance(payload, dict) else None
            if (not isinstance(payload, dict) or payload.get("success") is not True
                    or not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict)
                    or "expires" not in files[0] or files[0]["expires"] is not None):
                raise JournalError("Exit chart host rejected the upload; pending journal updates will retry.")
            url = files[0].get("url")
            if not isinstance(url, str) or not QUAX_URL.fullmatch(url):
                raise JournalError("Exit chart host returned an invalid chart reference; sync will retry.")
            return url
        except JournalError:
            raise
        except Exception:
            raise JournalError("Exit chart hosted upload failed; pending journal updates will retry.") from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def _save_url(self, path: Path, url: str, digest: str) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".exit-chart-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"url": url, "sha256": digest}, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        except Exception:
            raise JournalError("Could not save the exit chart upload reference; sync will retry.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def close(self) -> None:
        self.session.close()
        if self.public_session is not self.session:
            self.public_session.close()


class GCSChartPublisher:
    """Create a chart once, then verify the exact image is anonymously readable.

    The bucket's public-read policy is an explicit deployment prerequisite. This
    class never changes bucket permissions or object ACLs. Deterministic object
    names plus a generation precondition make retries safe after an interruption.
    """

    def __init__(self, settings: JournalSettings, *, session=None, public_session=None,
                 timeout: int = 15) -> None:
        bucket = getattr(settings, "chart_bucket", "")
        if not bucket:
            raise JournalError("Set ACTIVE_TRADES_JOURNAL_CHART_BUCKET to the dedicated chart bucket.")
        self.bucket = bucket
        self.timeout = timeout
        if session is None:
            try:
                from google.auth.transport.requests import AuthorizedSession
                from google.oauth2.service_account import Credentials
            except ImportError:
                raise JournalError('Install chart support with: pip install -e ".[journal]"') from None
            try:
                credentials = Credentials.from_service_account_file(
                    str(settings.credentials_path), scopes=[STORAGE_SCOPE],
                )
                session = AuthorizedSession(credentials)
            except Exception:
                raise JournalError("Could not load the Google service-account credentials file.") from None
        self.session = session
        if public_session is None:
            try:
                public_session = _public_session()
            except JournalError:
                self.session.close()
                raise
        self.public_session = public_session

    def publish(self, path: Path, object_key: str) -> str:
        if not object_key or not isinstance(object_key, str):
            raise JournalError("Exit chart storage requires a nonempty object name.")
        data = _read_png(path)
        upload_url = "https://storage.googleapis.com/upload/storage/v1/b/" + quote(self.bucket, safe="") + "/o"
        try:
            response = self.session.post(
                upload_url,
                params={"uploadType": "media", "name": object_key, "ifGenerationMatch": "0"},
                data=data, headers={"Content-Type": "image/png"},
                timeout=self.timeout, allow_redirects=False,
            )
            status = response.status_code
            response.close()
        except Exception:
            raise JournalError("Exit chart upload failed; pending journal updates will retry.") from None
        if not 200 <= status < 300 and status != 412:
            if status == 403:
                detail = "grant the service account Storage Object Creator on the chart bucket"
            elif status == 404:
                detail = "check ACTIVE_TRADES_JOURNAL_CHART_BUCKET and the bucket's existence"
            elif status == 401:
                detail = "check the service-account credentials and system clock"
            elif status == 429:
                detail = "request quota exceeded; pending journal updates will retry"
            else:
                detail = "request rejected; pending journal updates will retry"
            raise JournalError(f"Exit chart upload HTTP {status}; {detail}.")
        url = "https://storage.googleapis.com/" + quote(self.bucket, safe="") + "/" + quote(object_key, safe="/")
        self._verify_public(url, data)
        return url

    def _verify_public(self, url: str, expected: bytes) -> None:
        _verify_public(
            self.public_session, url, expected, self.timeout,
            unreadable="Exit chart is not publicly readable; configure public object reads on the dedicated "
                       "chart bucket. The chart cell will retry after access is corrected.",
        )

    def close(self) -> None:
        self.session.close()
        if self.public_session is not self.session:
            self.public_session.close()
