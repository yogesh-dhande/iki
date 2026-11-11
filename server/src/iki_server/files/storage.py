"""Object storage: the bucket Iki keeps files in, and signed URLs for reaching it.

Iki talks to the store with its own credentials. People and workers never
get credentials: they get URLs signed for one object and one operation, that
expire. The keys under ``blobs/`` hold checked content. Only the server
writes them, after it has checked the bytes, so a signed URL never names
one for writing.
"""

from __future__ import annotations

import hashlib
from functools import cache
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

import boto3
from botocore.config import Config
from django.conf import settings

if TYPE_CHECKING:
    from types_boto3_s3 import S3Client

#: How long a signed URL works. Uploads ask for more as they go.
UPLOAD_URL_SECONDS = 3600
DOWNLOAD_URL_SECONDS = 300
#: S3's limits: parts of at least 5 MiB (but the last), and at most 10,000 of them.
MIN_PART_SIZE = 5 * 1024**2
MAX_PARTS = 10_000
DEFAULT_PART_SIZE = 64 * 1024**2
READ_CHUNK = 8 * 1024**2


class NotConfigured(Exception):  # noqa: N818 - reads as "storage is not configured"
    def __init__(self) -> None:
        super().__init__("File storage isn’t set up: your administrator sets IKI_FILES_BUCKET.")


def configured() -> bool:
    return bool(settings.IKI_FILES_BUCKET)


def bucket() -> str:
    if not configured():
        raise NotConfigured
    return str(settings.IKI_FILES_BUCKET)


def _key(*parts: str) -> str:
    prefix = settings.IKI_FILES_PREFIX
    return "/".join([prefix, *parts] if prefix else parts)


def staging_key(upload_id: Any) -> str:  # noqa: ANN401 - a UUID
    return _key("uploads", str(upload_id))


def blob_key(sha256: str) -> str:
    return _key("blobs", "sha256", sha256[:2], sha256)


def uri(key: str) -> str:
    return f"s3://{bucket()}/{key}"


def _client(endpoint: str, region: str = "") -> S3Client:
    return boto3.client(
        "s3",
        region_name=region or settings.IKI_FILES_REGION,
        endpoint_url=endpoint or None,
        config=Config(
            signature_version="s3v4",
            # S3-compatible stores are reached by path; AWS by the bucket's own host name.
            s3={"addressing_style": "path" if endpoint else "virtual"},
            retries={"mode": "standard", "max_attempts": 5},
        ),
    )


@cache
def client() -> S3Client:
    """For the server's own requests to the store."""
    return _client(settings.IKI_FILES_ENDPOINT_URL)


def _endpoint_for(*, workers: bool) -> str:
    """The address URLs are signed for: where people's browsers reach the store, or
    where workers do, if that's elsewhere (such as the store's address in the cluster)."""
    public = str(settings.IKI_FILES_PUBLIC_ENDPOINT_URL)
    return str(settings.IKI_FILES_WORKER_ENDPOINT_URL or public) if workers else public


@cache
def _signer(endpoint: str) -> S3Client:
    return _client(endpoint)


def signer(*, workers: bool = False) -> S3Client:
    """For URLs that browsers, or workers, use: signed for the address they reach."""
    return _signer(_endpoint_for(workers=workers))


@cache
def region_of(bucket_name: str) -> str:
    """The bucket's region, which requests to it must be signed for.

    S3-compatible stores have one region (the configured one). On AWS,
    registered buckets can be anywhere.
    """
    if settings.IKI_FILES_ENDPOINT_URL or bucket_name == settings.IKI_FILES_BUCKET:
        return str(settings.IKI_FILES_REGION)
    found = client().get_bucket_location(Bucket=bucket_name).get("LocationConstraint")
    # Buckets made before regions had names answer None (us-east-1) or "EU" (eu-west-1).
    return {None: "us-east-1", "": "us-east-1", "EU": "eu-west-1"}.get(found, str(found))


@cache
def _regional(endpoint: str, region: str) -> S3Client:
    return _client(endpoint, region)


def client_for(bucket_name: str) -> S3Client:
    """For the server's own requests to any bucket: its own, or a registered one."""
    region = region_of(bucket_name)
    if region == settings.IKI_FILES_REGION:
        return client()
    return _regional(settings.IKI_FILES_ENDPOINT_URL, region)


def signer_for(bucket_name: str, *, workers: bool = False) -> S3Client:
    region = region_of(bucket_name)
    if region == settings.IKI_FILES_REGION:
        return signer(workers=workers)
    return _regional(_endpoint_for(workers=workers), region)


def reset() -> None:
    """Forgets the clients, after settings change (tests)."""
    for cached in (client, _signer, region_of, _regional, upload_origin):
        cached.cache_clear()


# --- Uploads -------------------------------------------------------------------------------------


def part_size_for(size: int) -> int:
    """Parts of 64 MiB, or larger so that no upload needs more than 10,000."""
    needed = -(-size // MAX_PARTS)
    mib = 1024**2
    return max(DEFAULT_PART_SIZE, -(-needed // mib) * mib)


def part_count(size: int, part_size: int) -> int:
    return max(1, -(-size // part_size))


def start_multipart(key: str) -> str:
    return client().create_multipart_upload(Bucket=bucket(), Key=key)["UploadId"]


@cache
def upload_origin() -> str | None:
    """Where browsers send upload parts (the origin part URLs are signed for), for the CSP.

    None until storage is configured."""
    if not configured():
        return None
    url = signer().generate_presigned_url(
        "upload_part",
        Params={"Bucket": bucket(), "Key": "origin", "UploadId": "origin", "PartNumber": 1},
        ExpiresIn=60,
    )
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def part_url(key: str, multipart_id: str, number: int, *, workers: bool = False) -> str:
    return signer(workers=workers).generate_presigned_url(
        "upload_part",
        Params={"Bucket": bucket(), "Key": key, "UploadId": multipart_id, "PartNumber": number},
        ExpiresIn=UPLOAD_URL_SECONDS,
    )


def uploaded_parts(key: str, multipart_id: str) -> dict[int, list[tuple[int, str]]]:
    """The parts that arrived: number → [(size, ETag)], newest first.

    A part sent again replaces the earlier one. S3 lists only the latest, but
    some compatible stores list every copy, in no reliable order."""
    found: dict[int, list[tuple[Any, int, str]]] = {}
    marker = 0
    while True:
        page = client().list_parts(
            Bucket=bucket(), Key=key, UploadId=multipart_id, PartNumberMarker=marker
        )
        for part in page.get("Parts", []):
            found.setdefault(part["PartNumber"], []).append(
                (part.get("LastModified"), part["Size"], part["ETag"])
            )
        if not page.get("IsTruncated"):
            break
        marker = page["NextPartNumberMarker"]
    return {
        number: [(size, etag) for _, size, etag in sorted(copies, key=lambda c: c[0], reverse=True)]
        for number, copies in found.items()
    }


def complete_multipart(key: str, multipart_id: str, etags: dict[int, str]) -> None:
    client().complete_multipart_upload(
        Bucket=bucket(),
        Key=key,
        UploadId=multipart_id,
        MultipartUpload={"Parts": [{"PartNumber": n, "ETag": etags[n]} for n in sorted(etags)]},
    )


def abort_multipart(key: str, multipart_id: str) -> None:
    try:
        client().abort_multipart_upload(Bucket=bucket(), Key=key, UploadId=multipart_id)
    except client().exceptions.NoSuchUpload:
        pass


def put_empty(key: str) -> None:
    client().put_object(Bucket=bucket(), Key=key, Body=b"")


# --- Reading and moving --------------------------------------------------------------------------


def digest(key: str) -> tuple[str, int]:
    """The object's SHA-256 and size, from its bytes."""
    return digest_object(bucket(), key)


def digest_object(bucket_name: str, key: str, version: str = "") -> tuple[str, int]:
    """The SHA-256 and size of any object (at ``version``, if given), from its bytes."""
    params: dict[str, Any] = {"Bucket": bucket_name, "Key": key}
    if version:
        params["VersionId"] = version
    body = client_for(bucket_name).get_object(**params)["Body"]
    sha = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: body.read(READ_CHUNK), b""):
        sha.update(chunk)
        size += len(chunk)
    return sha.hexdigest(), size


def exists(key: str) -> bool:
    try:
        client().head_object(Bucket=bucket(), Key=key)
    except client().exceptions.ClientError as error:
        if error.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise
    return True


def split_uri(storage_uri: str) -> tuple[str, str]:
    """``s3://bucket/some/key`` → ``("bucket", "some/key")``."""
    bucket_name, _, key = storage_uri.removeprefix("s3://").partition("/")
    return bucket_name, key


def versioned(bucket_name: str) -> bool:
    """Whether the bucket keeps every version of its objects."""
    status = client_for(bucket_name).get_bucket_versioning(Bucket=bucket_name).get("Status")
    return status == "Enabled"


def head(bucket_name: str, key: str, version: str = "") -> dict[str, Any] | None:
    """The object's size, version and content type, or None if there's no such object."""
    params: dict[str, Any] = {"Bucket": bucket_name, "Key": key}
    if version:
        params["VersionId"] = version
    try:
        found = client_for(bucket_name).head_object(**params)
    except client().exceptions.ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey", "NotFound", "NoSuchVersion", "400"):
            return None
        raise
    return {
        "size": found["ContentLength"],
        "version": found.get("VersionId") or "",
        "media_type": found.get("ContentType", ""),
    }


def copy(source: str, target: str) -> None:
    """Copies within the bucket; large objects are copied in parts, by the store."""
    client().copy({"Bucket": bucket(), "Key": source}, bucket(), target)


def delete(key: str) -> None:
    client().delete_object(Bucket=bucket(), Key=key)


def download_url(
    storage_uri: str, version: str, name: str, media_type: str, *, workers: bool = False
) -> str:
    """A short-lived URL that downloads the object, saved under ``name``: for a worker,
    signed for the address workers reach the store at."""
    bucket_name, key = split_uri(storage_uri)
    params: dict[str, Any] = {
        "Bucket": bucket_name,
        "Key": key,
        "ResponseContentType": media_type,
        # Always a download, never shown in the page: the bytes are whatever was uploaded.
        "ResponseContentDisposition": content_disposition(name),
    }
    if version:
        params["VersionId"] = version
    return signer_for(bucket_name, workers=workers).generate_presigned_url(
        "get_object", Params=params, ExpiresIn=DOWNLOAD_URL_SECONDS
    )


def content_disposition(name: str) -> str:
    """``attachment`` with the name as plain ASCII and as UTF-8 (RFC 6266)."""
    ascii_name = "".join(
        c if c.isascii() and c.isprintable() and c not in '"\\' else "_" for c in name
    )
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"
