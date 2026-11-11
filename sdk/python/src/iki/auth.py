"""Signing in, and keeping the sign-in.

``iki.login()`` signs in with the OAuth device flow (RFC 8628), as Iki's CLI does: it
shows a code, you enter it in Iki in your browser and choose what to allow, and the
SDK gets a token for you. You sign in to Iki there, with your organization's single
sign-on, so the SDK never sees your password.

Sign-ins are kept in your system's keychain (macOS Keychain, Windows Credential
Manager, or the Secret Service on Linux). Where there is none, as on many servers and
clusters, they're kept in a file only you can read. Access tokens last an hour and are
renewed as needed; renewing replaces the refresh token, so a stolen old one is no use.

For scripts and pipelines, set ``IKI_URL`` and ``IKI_TOKEN`` (a service account's
token, say) instead: nothing is stored then.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import webbrowser
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2

from iki.errors import IkiError, NotSignedIn, SignInFailed

__all__ = ["CLIENT_ID", "DEFAULT_SCOPES", "Credentials", "Store", "config_dir", "normalize_url"]

#: Iki's own public client for its CLI and this SDK.
CLIENT_ID = "iki-cli"
DEFAULT_SCOPES = ("read", "write", "run")
DEVICE_CODE = "urn:ietf:params:oauth:grant-type:device_code"
#: Renew an access token this many seconds before it expires.
RENEW_EARLY = 60
_KEYRING_SERVICE = "ikisci"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def config_dir() -> Path:
    """Where the SDK keeps its settings: ``IKI_CONFIG_DIR``, else the platform's own."""
    if named := os.environ.get("IKI_CONFIG_DIR"):
        return Path(named)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "iki"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "iki"


def normalize_url(url: str) -> str:
    """An Iki deployment's address, as the SDK keeps it: ``https://iki.example.com``.

    Plain HTTP is refused except on this machine, so a token is never sent where it
    could be read on the way.
    """
    text = url.strip()
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("https", "http") or not host:
        raise IkiError(f"{url!r} isn't an Iki address, such as https://iki.example.com.")
    local = host in _LOCAL_HOSTS or host.endswith(".localhost")
    if parts.scheme == "http" and not local:
        raise IkiError(
            f"{url} uses plain HTTP, where your sign-in could be read on the way: "
            "use its https:// address."
        )
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise IkiError(f"{url!r} should be the deployment's address only, without a path.")
    return f"{parts.scheme}://{parts.netloc.lower()}"


@dataclass(frozen=True)
class Credentials:
    """A sign-in to one deployment."""

    url: str
    access_token: str
    refresh_token: str
    #: When the access token expires, in seconds since the epoch.
    expires_at: float
    #: What the person allowed, space-separated: ``read write run``.
    scope: str

    def expiring(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.expires_at - RENEW_EARLY

    @classmethod
    def from_token_response(cls, url: str, body: dict[str, Any]) -> Credentials:
        return cls(
            url=url,
            access_token=str(body["access_token"]),
            refresh_token=str(body.get("refresh_token") or ""),
            expires_at=time.time() + float(body.get("expires_in") or 3600),
            scope=str(body.get("scope") or ""),
        )


def _keyring() -> Any | None:  # noqa: ANN401 - keyring's backend
    """The system keychain, if there's one to use."""
    try:
        import keyring
        from keyring.backends import fail

        backend = keyring.get_keyring()
    except Exception:
        return None
    if isinstance(backend, fail.Keyring) or getattr(backend, "priority", 1) <= 0:
        return None
    return backend


class Store:
    """Where sign-ins are kept: the system keychain, else a file only you can read."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or config_dir()

    @property
    def file(self) -> Path:
        return self.directory / "credentials.json"

    def where(self) -> str:
        """Where a sign-in saved now is kept, for people."""
        return "your system keychain" if _keyring() is not None else str(self.file)

    def load(self, url: str) -> Credentials | None:
        backend = _keyring()
        if backend is not None:
            with contextlib.suppress(Exception):
                text = backend.get_password(_KEYRING_SERVICE, url)
                if text:
                    return Credentials(**json.loads(text))
        found = self._read_file().get(url)
        return Credentials(**found) if found else None

    def save(self, credentials: Credentials) -> str:
        """Keeps `credentials`, and says where."""
        backend = _keyring()
        if backend is not None:
            try:
                backend.set_password(
                    _KEYRING_SERVICE, credentials.url, json.dumps(asdict(credentials))
                )
            except Exception:  # noqa: S110 - a locked or broken keychain: use the file
                pass
            else:
                self._forget_in_file(credentials.url)
                return "your system keychain"
        saved = self._read_file()
        saved[credentials.url] = asdict(credentials)
        self._write_file(saved)
        return str(self.file)

    def delete(self, url: str) -> None:
        backend = _keyring()
        if backend is not None:
            with contextlib.suppress(Exception):
                backend.delete_password(_KEYRING_SERVICE, url)
        self._forget_in_file(url)

    def _forget_in_file(self, url: str) -> None:
        saved = self._read_file()
        if url in saved:
            del saved[url]
            self._write_file(saved)

    def _read_file(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.file.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_file(self, data: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            self.directory.chmod(0o700)
        temporary = self.file.with_suffix(".tmp")
        # Created readable only by you, then moved into place whole.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as out:
            json.dump(data, out, indent=2)
        temporary.replace(self.file)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Held while renewing, so two processes never renew with the same token: a
        refresh token used twice ends the sign-in."""
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / ".lock").open("a+b") as handle:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                        break
                    except OSError:  # it gives up after 10 seconds; keep waiting
                        continue
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _remember_url(url: str, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(json.dumps({"url": url}, indent=2))


def remembered_url(directory: Path | None = None) -> str | None:
    """The deployment you last signed in to."""
    try:
        data = json.loads(((directory or config_dir()) / "config.json").read_text())
    except (OSError, ValueError):
        return None
    url = data.get("url") if isinstance(data, dict) else None
    return url if isinstance(url, str) else None


def _can_open_browser() -> bool:
    """Whether a browser would open in front of the person, not on a server they've
    connected to, nor as a text browser taking over the terminal."""
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    system: str = sys.platform  # read at run time, not as the type checker's platform
    if system in ("darwin", "win32"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _oauth_error(response: httpx2.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    return str(body.get("error") or "") if isinstance(body, dict) else ""


def device_login(
    http: httpx2.Client,
    url: str,
    *,
    scopes: Sequence[str],
    open_browser: bool,
    say: Callable[[str], None],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Credentials:
    """Signs in to `url` with the device flow."""
    started = http.post(
        f"{url}/oauth/device-authorization/",
        data={"client_id": CLIENT_ID, "scope": " ".join(scopes)},
    )
    if started.status_code != 200:
        error = _oauth_error(started)
        if error:
            # Iki answered, but won't let this app sign in: an administrator can say why.
            raise SignInFailed(
                f"{url} refused to start a sign-in: {error}. Ask your Iki administrator."
            )
        raise SignInFailed(
            f"{url} didn't start a sign-in ({started.status_code}). Is it an Iki deployment?"
        )
    grant = started.json()
    code, page = str(grant["user_code"]), str(grant["verification_uri"])
    direct = str(grant.get("verification_uri_complete") or page)
    say(f"To sign in to Iki, open {page} and enter the code {code}")
    if open_browser and _can_open_browser():
        with contextlib.suppress(Exception):
            webbrowser.open(direct)
    interval = float(grant.get("interval") or 5)
    deadline = clock() + float(grant.get("expires_in") or 900)
    while clock() < deadline:
        sleep(interval)
        polled = http.post(
            f"{url}/oauth/token/",
            data={
                "grant_type": DEVICE_CODE,
                "device_code": grant["device_code"],
                "client_id": CLIENT_ID,
            },
        )
        if polled.status_code == 200:
            return Credentials.from_token_response(url, polled.json())
        match _oauth_error(polled):
            case "authorization_pending":
                continue
            case "slow_down":
                interval += 5
            case "access_denied":
                raise SignInFailed("The sign-in was declined in the browser.")
            case "expired_token":
                break
            case other:
                raise SignInFailed(f"Signing in failed: {other or polled.status_code}.")
    raise SignInFailed(f"The code {code} expired before it was entered. Run iki.login() again.")


def renew(http: httpx2.Client, credentials: Credentials, store: Store) -> Credentials:
    """A fresh access token for `credentials`, renewing it once across processes."""
    with store.lock():
        current = store.load(credentials.url) or credentials
        # Another process renewed it while we waited for the lock: use theirs.
        if current.refresh_token != credentials.refresh_token and not current.expiring():
            return current
        if not current.refresh_token:
            raise NotSignedIn(f"Your sign-in to {credentials.url} has expired: run iki.login().")
        renewed = http.post(
            f"{credentials.url}/oauth/token/",
            data={
                "grant_type": "refresh_token",
                "refresh_token": current.refresh_token,
                "client_id": CLIENT_ID,
            },
        )
        if renewed.status_code != 200:
            if _oauth_error(renewed) in ("invalid_grant", "invalid_client"):
                store.delete(credentials.url)
                raise NotSignedIn(
                    f"Your sign-in to {credentials.url} has expired or was ended: "
                    "run iki.login() again."
                )
            renewed.raise_for_status()
        fresh = Credentials.from_token_response(credentials.url, renewed.json())
        if not fresh.refresh_token:
            fresh = Credentials(**{**asdict(fresh), "refresh_token": current.refresh_token})
        store.save(fresh)
        return fresh


def revoke(http: httpx2.Client, credentials: Credentials) -> None:
    """Ends a sign-in at the server: its refresh token, and the access tokens from it."""
    for token, hint in (
        (credentials.refresh_token, "refresh_token"),
        (credentials.access_token, "access_token"),
    ):
        if token:
            with contextlib.suppress(httpx2.HTTPError):
                http.post(
                    f"{credentials.url}/oauth/revoke/",
                    data={"token": token, "token_type_hint": hint, "client_id": CLIENT_ID},
                )
