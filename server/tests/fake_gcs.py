"""A small, strict Google Cloud Storage for tests, in this process.

It runs a real HTTP server, so Iki's Cloud Storage backend and Google's client make
real requests, and answers the little of Google's APIs that Iki uses, as Google
documents them:

- The JSON API (``/storage/v1``, ``/upload/storage/v1``, ``/download/storage/v1``):
  buckets' settings, objects' metadata, media and multipart and resumable uploads,
  versioned listings, deleting a generation, rewrites, and holds and retention, which
  refuse a delete or a replacement as Google does.
- The XML API's multipart uploads (``POST ?uploads``, ``PUT ?partNumber&uploadId``,
  ``GET ?uploadId``, ``POST ?uploadId``, ``DELETE ?uploadId``), and reads.
- V4 signed URLs (``GOOG4-RSA-SHA256``): it checks each one's signature against the
  public key of the service account that signed it, and its expiry, so a URL Iki
  signed wrong is refused as Google would refuse it.
- IAM Credentials' ``signBlob``, which signs as that service account, with a key only
  this fake has: Iki's backend signs URLs without a key of its own, as on GKE.

Every request must carry the bearer token :attr:`FakeGcs.token`, or be signed.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.parser import BytesParser
from email.policy import HTTP
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlsplit
from xml.etree import ElementTree

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

#: The XML API's namespace, as Google's responses give it.
XMLNS = "http://s3.amazonaws.com/doc/2006-03-01/"
EMAIL = "iki-server@iki-test.iam.gserviceaccount.com"


@dataclass
class Version:
    """One generation of an object."""

    data: bytes
    generation: int
    content_type: str
    updated: str
    live: bool = True
    temporary_hold: bool = False
    event_based_hold: bool = False
    retention: dict[str, str] | None = None

    def held(self) -> str | None:
        """Why it can't be deleted or replaced, if it can't."""
        if self.temporary_hold or self.event_based_hold:
            return "Object is under active hold."
        until = (self.retention or {}).get("retainUntilTime")
        if until and datetime.fromisoformat(until) > datetime.now(UTC):
            return "Object is under active retention."
        return None


@dataclass
class Bucket:
    name: str
    versioning: bool = True
    object_retention: bool = True
    kms_key: str = "projects/p/locations/eu/keyRings/iki/cryptoKeys/files"
    cors: list[dict[str, Any]] = field(default_factory=list)
    lifecycle: list[dict[str, Any]] = field(default_factory=list)
    #: Each object's generations, oldest first.
    objects: dict[str, list[Version]] = field(default_factory=dict)


@dataclass
class Multipart:
    bucket: str
    key: str
    content_type: str
    #: Each part number's copies, as (when, data, ETag), latest last.
    parts: dict[int, list[tuple[str, bytes, str]]] = field(default_factory=dict)


@dataclass
class FakeGcs:
    port: int = 0
    token: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    key: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    buckets: dict[str, Bucket] = field(default_factory=dict)
    multiparts: dict[str, Multipart] = field(default_factory=dict)
    resumable: dict[str, tuple[str, str, str, bytearray]] = field(default_factory=dict)
    #: How many blobs it signed for whom, and every request's method and path, to assert on.
    signed: list[str] = field(default_factory=list)
    requests: list[tuple[str, str]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    _generation: int = 1_700_000_000_000_000

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def bucket(self, name: str) -> Bucket:
        return self.buckets.setdefault(name, Bucket(name))

    def next_generation(self) -> int:
        self._generation += 1
        return self._generation

    def live(self, bucket: str, key: str) -> Version | None:
        found = [v for v in self.bucket(bucket).objects.get(key, []) if v.live]
        return found[-1] if found else None

    def generation(self, bucket: str, key: str, generation: str | None) -> Version | None:
        if not generation:
            return self.live(bucket, key)
        if not generation.isdigit():
            return None
        for version in self.bucket(bucket).objects.get(key, []):
            if version.generation == int(generation):
                return version
        return None

    def write(self, bucket: str, key: str, data: bytes, content_type: str) -> Version:
        """A new live generation, the old one noncurrent (or gone, unversioned)."""
        place = self.bucket(bucket)
        have = self.live(bucket, key)
        if have is not None and (why := have.held()):
            raise Refused(403, why)
        versions = place.objects.setdefault(key, [])
        if have is not None:
            have.live = False
            if not place.versioning:
                versions.remove(have)
        made = Version(
            data,
            self.next_generation(),
            content_type or "application/octet-stream",
            datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
        versions.append(made)
        return made

    def resource(self, bucket: str, key: str, version: Version) -> dict[str, Any]:
        found: dict[str, Any] = {
            "kind": "storage#object",
            "id": f"{bucket}/{key}/{version.generation}",
            "bucket": bucket,
            "name": key,
            "generation": str(version.generation),
            "metageneration": "1",
            "contentType": version.content_type,
            "size": str(len(version.data)),
            "md5Hash": base64.b64encode(hashlib.md5(version.data).digest()).decode(),  # noqa: S324 - Google's
            "updated": version.updated,
            "timeCreated": version.updated,
            "kmsKeyName": self.bucket(bucket).kms_key + "/cryptoKeyVersions/1",
            "temporaryHold": version.temporary_hold,
            "eventBasedHold": version.event_based_hold,
        }
        if not version.live:
            found["timeDeleted"] = version.updated
        if version.retention:
            found["retention"] = version.retention
        return found

    def bucket_resource(self, bucket: Bucket) -> dict[str, Any]:
        return {
            "kind": "storage#bucket",
            "name": bucket.name,
            "id": bucket.name,
            "versioning": {"enabled": bucket.versioning},
            "objectRetention": {"mode": "Enabled"} if bucket.object_retention else {},
            "encryption": {
                "defaultKmsKeyName": bucket.kms_key,
                "googleManagedEncryptionEnforcementConfig": {"restrictionMode": "FullyRestricted"},
            },
            "cors": bucket.cors,
            "lifecycle": {"rule": bucket.lifecycle},
            "metageneration": "1",
        }

    # --- Signed URLs ---------------------------------------------------------------------

    def check_signature(self, method: str, path: str, query: str, headers: Any) -> str | None:
        """Why a V4-signed request is refused, or None if its signature and time are good."""
        pairs = parse_qsl(query, keep_blank_values=True)
        given = dict(pairs)
        signature = given.get("X-Goog-Signature", "")
        if given.get("X-Goog-Algorithm") != "GOOG4-RSA-SHA256":
            return "Only GOOG4-RSA-SHA256 is accepted here."
        email, _, scope = given.get("X-Goog-Credential", "").partition("/")
        if email != EMAIL:
            return f"Signed by {email!r}, whose key this store doesn't have."
        signed_at = datetime.strptime(given["X-Goog-Date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        if time.time() > signed_at.timestamp() + int(given["X-Goog-Expires"]):
            return "The signed URL has expired."
        canonical_query = "&".join(
            sorted(
                f"{quote(k, safe='~')}={quote(v, safe='~')}"
                for k, v in pairs
                if k != "X-Goog-Signature"
            )
        )
        names = given["X-Goog-SignedHeaders"].split(";")
        canonical_headers = "".join(f"{n}:{(headers.get(n) or '').strip()}\n" for n in names)
        request = "\n".join(
            [method, path, canonical_query, canonical_headers, ";".join(names), "UNSIGNED-PAYLOAD"]
        )
        to_sign = "\n".join(
            [
                "GOOG4-RSA-SHA256",
                given["X-Goog-Date"],
                scope,
                hashlib.sha256(request.encode()).hexdigest(),
            ]
        )
        try:
            self.key.public_key().verify(
                bytes.fromhex(signature), to_sign.encode(), padding.PKCS1v15(), hashes.SHA256()
            )
        except (InvalidSignature, ValueError):
            return "The request signature we calculated does not match the signature you provided."
        return None

    def sign(self, payload: bytes) -> bytes:
        return self.key.sign(payload, padding.PKCS1v15(), hashes.SHA256())

    # --- Serving -------------------------------------------------------------------------

    def start(self) -> FakeGcs:
        fake = self

        class Handler(_Handler):
            store = fake

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


class Refused(Exception):  # noqa: N818 - Google's answer
    def __init__(self, status: int, message: str, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


_OBJECT = re.compile(r"^/(?:(upload|download)/)?storage/v1/b/([^/]+)(?:/o(?:/(.+?))?)?$")
_REWRITE = re.compile(r"^/storage/v1/b/([^/]+)/o/(.+?)/(?:rewriteTo|copyTo)/b/([^/]+)/o/(.+)$")
_SIGN = re.compile(r"^/v1/projects/-/serviceAccounts/([^/:]+):signBlob$")


class _Handler(http.server.BaseHTTPRequestHandler):
    store: FakeGcs
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        self._serve("GET")

    def do_POST(self) -> None:
        self._serve("POST")

    def do_PUT(self) -> None:
        self._serve("PUT")

    def do_PATCH(self) -> None:
        self._serve("PATCH")

    def do_DELETE(self) -> None:
        self._serve("DELETE")

    def _body(self) -> bytes:
        size = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(size) if size else b""

    def _send(
        self,
        status: int,
        body: bytes | dict[str, Any] | str = b"",
        content_type: str = "application/json",
        headers: dict[str, str] | None = None,
    ) -> None:
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _serve(self, method: str) -> None:
        parts = urlsplit(self.path)
        body = self._body()
        store = self.store
        store.requests.append((method, parts.path))
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        try:
            if "X-Goog-Signature" in query:
                why = store.check_signature(method, parts.path, parts.query, self.headers)
                if why:
                    raise Refused(403, why, "SignatureDoesNotMatch")
            elif self.headers.get("Authorization") != f"Bearer {store.token}":
                raise Refused(401, "Anonymous caller does not have access.", "AccessDenied")
            with store.lock:
                if found := _SIGN.match(parts.path):
                    self._sign_blob(unquote(found[1]), body)
                elif parts.path.startswith("/storage/v1/") or parts.path.startswith(
                    ("/upload/storage/v1/", "/download/storage/v1/")
                ):
                    self._json(method, parts.path, query, body)
                else:
                    self._xml(method, parts.path, query, body)
        except Refused as refused:
            if parts.path.startswith(("/storage/", "/upload/", "/download/", "/v1/")):
                self._send(
                    refused.status,
                    {"error": {"code": refused.status, "message": refused.message}},
                )
            else:
                self._send(
                    refused.status,
                    f"<?xml version='1.0' encoding='UTF-8'?><Error><Code>{refused.code}</Code>"
                    f"<Message>{refused.message}</Message></Error>",
                    "application/xml",
                )

    def _sign_blob(self, email: str, body: bytes) -> None:
        if email != EMAIL:
            raise Refused(403, f"Permission 'iam.serviceAccounts.signBlob' denied on {email}")
        payload = base64.b64decode(json.loads(body)["payload"])
        self.store.signed.append(email)
        signed = base64.b64encode(self.store.sign(payload)).decode()
        self._send(200, {"keyId": "1", "signedBlob": signed})

    # --- The JSON API --------------------------------------------------------------------

    def _json(self, method: str, path: str, query: dict[str, str], body: bytes) -> None:
        store = self.store
        if found := _REWRITE.match(path):
            source = store.generation(found[1], unquote(found[2]), query.get("sourceGeneration"))
            if source is None:
                raise Refused(404, "No such object.")
            made = store.write(found[3], unquote(found[4]), source.data, source.content_type)
            resource = store.resource(found[3], unquote(found[4]), made)
            size = str(len(source.data))
            if path.split("/")[-4] == "copyTo":
                self._send(200, resource)
                return
            answer = {"kind": "storage#rewriteResponse", "done": True, "resource": resource}
            self._send(200, answer | {"totalBytesRewritten": size, "objectSize": size})
            return
        found = _OBJECT.match(path)
        if found is None:
            raise Refused(404, f"Not found: {path}")
        kind, name, key = found[1], found[2], unquote(found[3]) if found[3] else None
        if name not in store.buckets:
            raise Refused(404, "The specified bucket does not exist.")
        bucket = store.bucket(name)
        if kind == "upload":
            self._upload(bucket, query, body)
        elif key is None and path.endswith("/o"):
            self._list(bucket, query)
        elif key is None:
            if method == "PATCH":
                given = json.loads(body or b"{}")
                if "versioning" in given:
                    bucket.versioning = bool((given["versioning"] or {}).get("enabled"))
                if "cors" in given:
                    bucket.cors = given["cors"] or []
                if "lifecycle" in given:
                    bucket.lifecycle = (given["lifecycle"] or {}).get("rule") or []
            self._send(200, store.bucket_resource(bucket))
        else:
            self._object(method, bucket, key, query, body, media=kind == "download")

    def _list(self, bucket: Bucket, query: dict[str, str]) -> None:
        prefix = query.get("prefix", "")
        versions = query.get("versions", "").lower() == "true"
        items = [
            self.store.resource(bucket.name, key, version)
            for key in sorted(bucket.objects)
            if key.startswith(prefix)
            for version in bucket.objects[key]
            if versions or version.live
        ]
        self._send(200, {"kind": "storage#objects", "items": items})

    def _object(
        self,
        method: str,
        bucket: Bucket,
        key: str,
        query: dict[str, str],
        body: bytes,
        *,
        media: bool,
    ) -> None:
        store = self.store
        version = store.generation(bucket.name, key, query.get("generation"))
        if version is None:
            raise Refused(404, "No such object: " + key)
        if method == "DELETE":
            if why := version.held():
                raise Refused(403, why)
            if query.get("generation") or not bucket.versioning:
                bucket.objects[key].remove(version)
            else:
                version.live = False
            self._send(204)
            return
        if method == "PATCH":
            given = json.loads(body or b"{}")
            if "temporaryHold" in given:
                version.temporary_hold = bool(given["temporaryHold"])
            if "eventBasedHold" in given:
                version.event_based_hold = bool(given["eventBasedHold"])
            if "retention" in given:
                if not bucket.object_retention:
                    raise Refused(400, "Object retention is not enabled on this bucket.")
                version.retention = given["retention"]
        if media or query.get("alt") == "media":
            self._media(version)
            return
        self._send(200, store.resource(bucket.name, key, version))

    def _media(self, version: Version) -> None:
        data = version.data
        headers = {"x-goog-generation": str(version.generation)}
        wanted = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range") or "")
        if wanted and data:
            start = int(wanted[1])
            end = int(wanted[2]) if wanted[2] else len(data) - 1
            if start >= len(data):
                raise Refused(416, "Requested range not satisfiable.", "InvalidRange")
            headers["Content-Range"] = f"bytes {start}-{min(end, len(data) - 1)}/{len(data)}"
            self._send(206, data[start : end + 1], version.content_type, headers)
            return
        crc = hashlib.md5(data).digest()  # noqa: S324 - Google's
        headers["x-goog-hash"] = f"md5={base64.b64encode(crc).decode()}"
        self._send(200, data, version.content_type, headers)

    def _upload(self, bucket: Bucket, query: dict[str, str], body: bytes) -> None:
        store = self.store
        kind = query.get("uploadType")
        if kind == "media":
            made = store.write(bucket.name, query["name"], body, self.headers["Content-Type"])
            self._send(200, store.resource(bucket.name, query["name"], made))
        elif kind == "multipart":
            message = BytesParser(policy=HTTP).parsebytes(
                f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + body
            )
            meta_part, media_part = list(message.iter_parts())
            meta = json.loads(bytes(meta_part.get_payload(decode=True)))
            data = bytes(media_part.get_payload(decode=True))
            content_type = meta.get("contentType") or media_part.get_content_type()
            made = store.write(bucket.name, meta["name"], data, content_type)
            self._send(200, store.resource(bucket.name, meta["name"], made))
        elif kind == "resumable" and "upload_id" not in query:
            meta = json.loads(body or b"{}")
            session = secrets.token_hex(8)
            content_type = (
                meta.get("contentType")
                or self.headers.get("X-Upload-Content-Type")
                or "application/octet-stream"
            )
            store.resumable[session] = (bucket.name, meta["name"], content_type, bytearray())
            location = (
                f"{store.url}/upload/storage/v1/b/{bucket.name}/o"
                f"?uploadType=resumable&upload_id={session}"
            )
            self._send(200, b"", headers={"Location": location})
        elif kind == "resumable":
            name, key, content_type, sent = store.resumable[query["upload_id"]]
            sent.extend(body)
            total = (self.headers.get("Content-Range") or "").rpartition("/")[2]
            if total == "*" or (total.isdigit() and len(sent) < int(total)):
                self._send(308, b"", headers={"Range": f"bytes=0-{len(sent) - 1}"})
                return
            del store.resumable[query["upload_id"]]
            made = store.write(name, key, bytes(sent), content_type)
            self._send(200, store.resource(name, key, made))
        else:
            raise Refused(400, f"Unsupported uploadType {kind!r}.")

    # --- The XML API ---------------------------------------------------------------------

    def _xml(self, method: str, path: str, query: dict[str, str], body: bytes) -> None:
        store = self.store
        name, _, raw_key = path.lstrip("/").partition("/")
        key = unquote(raw_key)
        if name not in store.buckets:
            raise Refused(404, "The specified bucket does not exist.", "NoSuchBucket")
        if method == "POST" and "uploads" in query:
            upload_id = secrets.token_urlsafe(24)
            content_type = self.headers.get("Content-Type") or "application/octet-stream"
            store.multiparts[upload_id] = Multipart(name, key, content_type)
            self._send(
                200,
                f'<?xml version="1.0" encoding="UTF-8"?><InitiateMultipartUploadResult '
                f'xmlns="{XMLNS}"><Bucket>{name}</Bucket><Key>{key}</Key>'
                f"<UploadId>{upload_id}</UploadId></InitiateMultipartUploadResult>",
                "application/xml",
            )
            return
        if "uploadId" in query:
            self._multipart(method, query, body)
            return
        if method == "GET":
            version = store.generation(name, key, query.get("generation"))
            if version is None:
                raise Refused(404, "The specified key does not exist.", "NoSuchKey")
            headers = {"x-goog-generation": str(version.generation)}
            if disposition := query.get("response-content-disposition"):
                headers["Content-Disposition"] = disposition
            content_type = query.get("response-content-type") or version.content_type
            self._send(200, version.data, content_type, headers)
            return
        raise Refused(405, f"{method} isn't served here.", "MethodNotAllowed")

    def _multipart(self, method: str, query: dict[str, str], body: bytes) -> None:
        store = self.store
        upload = store.multiparts.get(query["uploadId"])
        if upload is None:
            raise Refused(404, "The requested upload was not found.", "NoSuchUpload")
        if method == "PUT":
            number = int(query["partNumber"])
            if not 1 <= number <= 10_000:
                raise Refused(400, "partNumber must be 1 to 10,000.", "InvalidArgument")
            etag = f'"{hashlib.md5(body).hexdigest()}"'  # noqa: S324 - Google's ETags
            when = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
            upload.parts.setdefault(number, []).append((when, body, etag))
            self._send(200, b"", "text/html", {"ETag": etag})
        elif method == "GET":
            marker = int(query.get("part-number-marker") or 0)
            numbers = sorted(n for n in upload.parts if n > marker)
            page, more = numbers[:1000], len(numbers) > 1000
            parts = "".join(
                f"<Part><PartNumber>{n}</PartNumber><LastModified>{upload.parts[n][-1][0]}"
                f"</LastModified><ETag>{upload.parts[n][-1][2]}</ETag>"
                f"<Size>{len(upload.parts[n][-1][1])}</Size></Part>"
                for n in page
            )
            truncated = (
                f"<IsTruncated>true</IsTruncated><NextPartNumberMarker>{page[-1]}"
                "</NextPartNumberMarker>"
                if more
                else "<IsTruncated>false</IsTruncated>"
            )
            self._send(
                200,
                f'<?xml version="1.0" encoding="UTF-8"?><ListPartsResult xmlns="{XMLNS}">'
                f"<Bucket>{upload.bucket}</Bucket><Key>{upload.key}</Key>"
                f"<UploadId>{query['uploadId']}</UploadId>{truncated}{parts}</ListPartsResult>",
                "application/xml",
            )
        elif method == "POST":
            wanted = ElementTree.fromstring(body)  # noqa: S314 - the test's own request
            chosen: list[bytes] = []
            order: list[int] = []
            for part in wanted.iter("Part"):
                number = int(part.findtext("PartNumber") or 0)
                etag = part.findtext("ETag") or ""
                copy = next((c for c in upload.parts.get(number, []) if c[2] == etag), None)
                if copy is None:
                    raise Refused(400, f"Part {number} wasn't uploaded as {etag}.", "InvalidPart")
                order.append(number)
                chosen.append(copy[1])
            if order != sorted(order):
                raise Refused(400, "Parts are out of order.", "InvalidPartOrder")
            if any(len(data) < 5 * 1024**2 for data in chosen[:-1]):
                raise Refused(400, "A part but the last is under 5 MiB.", "InvalidArgument")
            made = store.write(upload.bucket, upload.key, b"".join(chosen), upload.content_type)
            del store.multiparts[query["uploadId"]]
            self._send(
                200,
                f'<?xml version="1.0" encoding="UTF-8"?><CompleteMultipartUploadResult '
                f'xmlns="{XMLNS}"><Bucket>{upload.bucket}</Bucket><Key>{upload.key}</Key>'
                f'<ETag>"{made.generation}"</ETag></CompleteMultipartUploadResult>',
                "application/xml",
                {"x-goog-generation": str(made.generation)},
            )
        elif method == "DELETE":
            del store.multiparts[query["uploadId"]]
            self._send(204, b"", "text/html")
        else:
            raise Refused(405, f"{method} isn't served here.", "MethodNotAllowed")
