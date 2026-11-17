"""Files in Google Cloud Storage, through its own APIs (ADR 0109): the same functions as
:mod:`iki_server.files.storage` has for S3, which calls these when ``IKI_FILES_BACKEND``
is ``gcs``.

**Credentials.** Google's default chain: on GKE, the pod's Google service account, by
Workload Identity. There's no key. URLs are signed (V4, ``GOOG4-RSA-SHA256``) by IAM
Credentials' ``signBlob``, as that service account, which needs
``iam.serviceAccounts.signBlob`` on itself. Credentials that have a key of their own (a
key file, in development) sign locally instead.

**Versions are generations.** Every object Cloud Storage keeps has a generation, a
number that names that version for good: ``head`` pins it, reads and downloads ask for
it, and :func:`delete_for_good` deletes every generation of an object, as S3's version
IDs and ``ListObjectVersions`` do. Cloud Storage has no delete markers. A deleted
generation is kept, restorable, for the bucket's soft-delete period, then gone.

**Uploads in parts** are the XML API's multipart uploads, which work as S3's do (parts
1 to 10,000 of at least 5 MiB but the last, listed, completed with their ETags, and
aborted): browsers and workers PUT each part to a URL signed for it, and the server
starts, lists, completes and aborts them with its own token. So the web app, workers
and the SDK upload to Cloud Storage as they do to S3.

**Holds and retention** are the bucket owner's (Object Retention Lock, temporary and
event-based holds): Iki sets none, and Cloud Storage refuses to delete or replace a
generation under one, as S3 refuses an Object Lock'd version. Iki's own retention and
legal holds (ADR 0068, 0092) are in its database, on either store.

**Encryption** is the bucket's: its default customer-managed key encrypts every object
Iki writes (uploads, multipart uploads and rewrites name no key), and its enforcement
settings refuse Google's own keys and a caller's.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import re
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from functools import cache
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree

import google.auth
import google.cloud.storage as google_storage
import requests
from django.conf import settings
from google.api_core import exceptions as google_errors
from google.auth import credentials as google_credentials
from google.auth import exceptions as auth_errors
from google.auth.transport.requests import AuthorizedSession, Request
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from iki_server.files.storage import StorageError

if TYPE_CHECKING:
    from google.cloud.storage.blob import Blob
    from google.cloud.storage.bucket import Bucket

#: Google's endpoints, unless IKI_FILES_ENDPOINT_URL names another (an emulator).
STORAGE = "https://storage.googleapis.com"
IAM_CREDENTIALS = "https://iamcredentials.googleapis.com"
#: signBlob needs a token for the cloud-platform scope; Workload Identity's tokens have it.
SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
#: Part URLs are signed this many at once: each is a call to IAM.
SIGNING_THREADS = 16
#: The XML API's namespace, which its responses' elements are in.
_NAMESPACE = re.compile(r"^\{[^}]*\}")


def find_credentials() -> google_credentials.Credentials:
    """Google's default credentials: on GKE, the pod's service account's, by Workload
    Identity."""
    found, _ = google.auth.default(scopes=list(SCOPES))
    return found


class IamSigner(google_credentials.Signing):
    """Signs as a service account through IAM Credentials' ``signBlob``, with no key."""

    def __init__(self, session: AuthorizedSession, email: str, endpoint: str) -> None:
        self._session = session
        self._email = email
        self._endpoint = endpoint

    @property
    def signer_email(self) -> str:
        return self._email

    @property
    def signer(self) -> Any:  # noqa: ANN401 - google.auth's interface
        return self

    def sign_bytes(self, message: bytes | str) -> bytes:
        payload = message.encode() if isinstance(message, str) else message
        try:
            response = self._session.post(
                f"{self._endpoint}/v1/projects/-/serviceAccounts/{quote(self._email)}:signBlob",
                json={"payload": base64.b64encode(payload).decode("ascii")},
                timeout=30,
            )
        except requests.RequestException as error:
            raise StorageError(0, "unreachable", str(error)[:500]) from error
        if response.status_code != 200:
            raise StorageError(response.status_code, "signBlob", response.text[:500])
        return base64.b64decode(response.json()["signedBlob"])


@dataclass
class Google:
    """The clients the server uses, made once (and again after :func:`reset`)."""

    credentials: google_credentials.Credentials
    client: google_storage.Client
    #: For the XML API's multipart uploads and IAM, with the same credentials.
    session: AuthorizedSession
    endpoint: str

    def signer(self) -> google_credentials.Signing:
        """What signs URLs: the credentials themselves if they have a key, otherwise IAM,
        as their service account."""
        if isinstance(self.credentials, google_credentials.Signing):
            return self.credentials
        if not self.credentials.valid:
            # Compute Engine's (GKE's) credentials learn their account's email as they do.
            self.credentials.refresh(Request())
        email = str(getattr(self.credentials, "service_account_email", "") or "")
        if "@" not in email:
            raise StorageError(
                0,
                "no_service_account",
                "Signing Cloud Storage URLs needs a service account's credentials "
                "(Workload Identity on GKE), not a person's.",
            )
        return IamSigner(self.session, email, IAM_CREDENTIALS)


def _session(credentials: google_credentials.Credentials) -> AuthorizedSession:
    session = AuthorizedSession(credentials)
    # Rate limits and Google's own errors are retried; a request that may have been
    # carried out (one read timing out) isn't, as completing an upload twice fails.
    retry = Retry(
        total=5,
        connect=5,
        read=0,
        status=5,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=None,
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


@cache
def clients() -> Google:
    credentials = find_credentials()
    endpoint = str(settings.IKI_FILES_ENDPOINT_URL or STORAGE).rstrip("/")
    client = google_storage.Client(
        # Iki names every bucket it uses, so needs no project.
        project=None,
        credentials=credentials,
        client_options={"api_endpoint": endpoint} if settings.IKI_FILES_ENDPOINT_URL else None,
    )
    return Google(credentials, client, _session(credentials), endpoint)


def reset() -> None:
    clients.cache_clear()
    upload_origin.cache_clear()


def _bucket(name: str) -> Bucket:
    return clients().client.bucket(name)


def _ours() -> Bucket:
    from iki_server.files import storage

    return _bucket(storage.bucket())


def _blob(bucket_name: str, key: str, version: str = "") -> Blob | None:
    """The object, at ``version`` if given; None for a version that can't be a generation."""
    if version and not version.isdigit():
        return None
    return _bucket(bucket_name).blob(key, generation=int(version) if version else None)


# --- Uploads in parts: the XML API -----------------------------------------------------------


def _object_url(key: str) -> str:
    from iki_server.files import storage

    return f"{clients().endpoint}/{storage.bucket()}/{quote(key, safe='/~')}"


def _xml(method: str, key: str, params: dict[str, str], body: bytes = b"") -> requests.Response:
    try:
        response = clients().session.request(
            method,
            _object_url(key),
            params=params,
            data=body,
            headers={"Content-Type": "application/xml"} if body else {},
            timeout=60,
        )
    except requests.RequestException as error:
        raise StorageError(0, "unreachable", str(error)[:500]) from error
    if response.status_code >= 300:
        code, message = _xml_error(response)
        raise StorageError(response.status_code, code, message)
    return response


def _xml_error(response: requests.Response) -> tuple[str, str]:
    try:
        found = ElementTree.fromstring(response.content)  # noqa: S314 - Google's answer, no DTDs
    except ElementTree.ParseError:
        return "", response.text[:500]
    values = {_NAMESPACE.sub("", e.tag): (e.text or "") for e in found.iter()}
    return values.get("Code", ""), values.get("Message", "")[:500]


def _elements(response: requests.Response) -> Iterable[tuple[str, ElementTree.Element]]:
    """Each element of an XML API answer, by its name without the namespace."""
    found = ElementTree.fromstring(response.content)  # noqa: S314 - Google's answer, no DTDs
    for element in found.iter():
        yield _NAMESPACE.sub("", element.tag), element


def _child(element: ElementTree.Element, name: str) -> str:
    for child in element:
        if _NAMESPACE.sub("", child.tag) == name:
            return child.text or ""
    return ""


def start_multipart(key: str) -> str:
    response = _xml("POST", key, {"uploads": ""})
    for name, element in _elements(response):
        if name == "UploadId" and element.text:
            return element.text
    raise StorageError(response.status_code, "no_upload_id", response.text[:500])


def _endpoint_for(*, workers: bool) -> str:
    """Where browsers, or workers, reach Cloud Storage: what URLs are signed for."""
    public = str(settings.IKI_FILES_PUBLIC_ENDPOINT_URL or clients().endpoint)
    return str(settings.IKI_FILES_WORKER_ENDPOINT_URL or public) if workers else public


@cache
def upload_origin() -> str | None:
    from iki_server.files import storage

    if not storage.configured():
        return None
    parts = urlsplit(_endpoint_for(workers=False))
    return f"{parts.scheme}://{parts.netloc}"


def _sign(
    blob: Blob,
    *,
    method: str,
    seconds: int,
    workers: bool,
    signer: google_credentials.Signing | None = None,
    **given: object,
) -> str:
    return str(
        blob.generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=seconds),
            method=method,
            api_access_endpoint=_endpoint_for(workers=workers),
            credentials=signer or clients().signer(),
            **given,
        )
    )


def part_url(key: str, multipart_id: str, number: int, *, workers: bool = False) -> str:
    return part_urls(key, multipart_id, [number], workers=workers)[number]


def part_urls(
    key: str, multipart_id: str, numbers: list[int], *, workers: bool = False
) -> dict[int, str]:
    """Each part's upload URL, signed by IAM a few at once."""
    from iki_server.files import storage

    signer = clients().signer()
    blob = _ours().blob(key)

    def one(number: int) -> str:
        return _sign(
            blob,
            method="PUT",
            seconds=storage.UPLOAD_URL_SECONDS,
            workers=workers,
            signer=signer,
            query_parameters={"partNumber": str(number), "uploadId": multipart_id},
        )

    if len(numbers) == 1:
        return {numbers[0]: one(numbers[0])}
    with ThreadPoolExecutor(max_workers=SIGNING_THREADS) as pool:
        return dict(zip(numbers, pool.map(one, numbers), strict=True))


def uploaded_parts(key: str, multipart_id: str) -> dict[int, list[tuple[int, str]]]:
    found: dict[int, list[tuple[str, int, str]]] = {}
    marker = "0"
    while True:
        response = _xml("GET", key, {"uploadId": multipart_id, "part-number-marker": marker})
        truncated, after = False, ""
        for name, element in _elements(response):
            if name == "Part":
                number = int(_child(element, "PartNumber"))
                found.setdefault(number, []).append(
                    (
                        _child(element, "LastModified"),
                        int(_child(element, "Size")),
                        _child(element, "ETag"),
                    )
                )
            elif name == "IsTruncated":
                truncated = (element.text or "").strip() == "true"
            elif name == "NextPartNumberMarker":
                after = (element.text or "").strip()
        if not truncated or not after:
            break
        marker = after
    return {
        number: [(size, etag) for _, size, etag in sorted(copies, key=lambda c: c[0], reverse=True)]
        for number, copies in found.items()
    }


def complete_multipart(key: str, multipart_id: str, etags: dict[int, str]) -> None:
    root = ElementTree.Element("CompleteMultipartUpload")
    for number in sorted(etags):
        part = ElementTree.SubElement(root, "Part")
        ElementTree.SubElement(part, "PartNumber").text = str(number)
        ElementTree.SubElement(part, "ETag").text = etags[number]
    _xml("POST", key, {"uploadId": multipart_id}, ElementTree.tostring(root))


def abort_multipart(key: str, multipart_id: str) -> None:
    try:
        _xml("DELETE", key, {"uploadId": multipart_id})
    except StorageError as error:
        if error.status != 404 and error.code != "NoSuchUpload":
            raise


def put_empty(key: str) -> None:
    _ours().blob(key).upload_from_string(b"", content_type="application/octet-stream")


# --- Reading and moving: the JSON API --------------------------------------------------------


class _Hashing:
    """A file that keeps only the SHA-256 and size of what's written to it."""

    def __init__(self) -> None:
        self.sha = hashlib.sha256()
        self.size = 0

    def write(self, chunk: bytes) -> int:
        self.sha.update(chunk)
        self.size += len(chunk)
        return len(chunk)


def digest_object(bucket_name: str, key: str, version: str = "") -> tuple[str, int]:
    blob = _blob(bucket_name, key, version)
    if blob is None:
        raise StorageError(404, "NoSuchVersion", f"{key} has no version {version!r}.")
    found = _Hashing()
    blob.download_to_file(found)
    return found.sha.hexdigest(), found.size


def open_object(storage_uri: str, version: str = "", offset: int = 0) -> Any:  # noqa: ANN401 - a BlobReader
    from iki_server.files import storage

    bucket_name, key = storage.split_uri(storage_uri)
    blob = _blob(bucket_name, key, version)
    if blob is None:
        raise StorageError(404, "NoSuchVersion", f"{key} has no version {version!r}.")
    reader = blob.open("rb", chunk_size=storage.READ_CHUNK)
    if offset:
        reader.seek(offset)
    return reader


def exists(key: str) -> bool:
    return bool(_ours().blob(key).exists())


def versioned(bucket_name: str) -> bool:
    return bool(clients().client.get_bucket(bucket_name).versioning_enabled)


def head(bucket_name: str, key: str, version: str = "") -> dict[str, Any] | None:
    if version and not version.isdigit():
        return None
    found = _bucket(bucket_name).get_blob(key, generation=int(version) if version else None)
    if found is None:
        return None
    return {
        "size": int(found.size or 0),
        "version": str(found.generation or ""),
        "media_type": found.content_type or "",
    }


def copy(source: str, target: str) -> None:
    """Within the bucket, by rewriting: Cloud Storage copies large objects in steps."""
    bucket = _ours()
    into, token = bucket.blob(target), None
    while True:
        token, _, _ = into.rewrite(bucket.blob(source), token=token)
        if token is None:
            return


def download_file(key: str, path: Any) -> None:  # noqa: ANN401 - a Path
    _ours().blob(key).download_to_filename(str(path))


def upload_file(path: Any, key: str, media_type: str) -> None:  # noqa: ANN401 - a Path
    """Stores a local file: a resumable upload if it's large."""
    _ours().blob(key).upload_from_filename(str(path), content_type=media_type)


def delete(key: str) -> None:
    with contextlib.suppress(google_errors.NotFound):
        _ours().blob(key).delete()


def delete_for_good(key: str) -> int:
    bucket = _ours()
    doomed = [
        found
        for found in clients().client.list_blobs(bucket, prefix=key, versions=True)
        if found.name == key
    ]
    for found in doomed:
        with contextlib.suppress(google_errors.NotFound):
            bucket.blob(key, generation=found.generation).delete()
    return len(doomed)


def download_url(
    storage_uri: str, version: str, name: str, media_type: str, *, workers: bool = False
) -> str:
    from iki_server.files import storage

    bucket_name, key = storage.split_uri(storage_uri)
    given: dict[str, Any] = {
        "response_type": media_type,
        "response_disposition": storage.content_disposition(name),
    }
    if version:
        given["generation"] = version
    return _sign(
        _bucket(bucket_name).blob(key),
        method="GET",
        seconds=storage.DOWNLOAD_URL_SECONDS,
        workers=workers,
        **given,
    )


#: What Google's clients raise when Cloud Storage or IAM refuse or fail a request: the
#: storage module turns them into :class:`StorageError`, as the API answers 503 for.
ERRORS = (google_errors.GoogleAPIError, auth_errors.GoogleAuthError, requests.RequestException)


def error_from(error: Exception) -> StorageError:
    """Google's error as a :class:`StorageError`: its HTTP status (0 if none) and kind."""
    status = (
        int(getattr(error, "code", 0) or 0)
        if isinstance(error, google_errors.GoogleAPIError)
        else 0
    )
    return StorageError(status, type(error).__name__, str(error)[:500])


#: What :mod:`iki_server.files.storage` calls here, when the backend is ``gcs``.
FUNCTIONS: dict[str, Callable[..., Any]] = {
    f.__name__: f
    for f in (
        start_multipart,
        part_url,
        part_urls,
        uploaded_parts,
        complete_multipart,
        abort_multipart,
        put_empty,
        digest_object,
        open_object,
        exists,
        versioned,
        head,
        copy,
        download_file,
        upload_file,
        delete,
        delete_for_good,
        download_url,
    )
}
