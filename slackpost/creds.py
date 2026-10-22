"""Read and decrypt credentials stored on disk by slackdump.

slackdump stores each authenticated workspace as an encrypted file in its
cache directory (``~/Library/Caches/slackdump`` on macOS).  The file layout is::

    [ 16-byte IV ][ AES-256-CFB ciphertext ]

The plaintext is JSON of the form::

    {"Token": "xoxc-...", "Cookie": [ ...http.Cookie objects... ]}

The key-derivation chain (reverse-engineered from rusq/encio + rusq/secure):

1. ``machine_id``        = OS-specific unique id (macOS: IOPlatformUUID)
2. ``protected``         = hex(HMAC-SHA256(key=machine_id, msg=appID))
3. ``key``               = PBKDF2-HMAC-SHA512(protected, salt, 4096, 32)
4. plaintext             = AES-256-CFB(key, iv)

This module only reads and decrypts; it never writes or prints secrets.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

# In newer versions of `cryptography`, CFB lives in the ``decrepit`` module.
try:  # pragma: no cover - depends on installed version
    from cryptography.hazmat.decrepit.ciphers.modes import CFB  # type: ignore
    _CFB_NOTE = None
except ImportError:  # pragma: no cover
    from cryptography.hazmat.primitives.ciphers.modes import CFB  # type: ignore
    _CFB_NOTE = "CFB is deprecated in this version of cryptography"


# Public constant from slackdump / encio (not a secret: it is in the source).
APP_ID = "76d19bf515c59483e8923fcad9f1b65025d445e71801688b7edfb9cc2e64497f"

# Fixed package salt used by rusq/secure v0.1.1 (also public source).
_SALT = bytes(
    [
        0x51, 0xFC, 0xD8, 0xF9, 0xAB, 0x85, 0x93, 0x5D, 0xD2, 0x85, 0x2E, 0x78,
        0x3F, 0x80, 0x3A, 0xCE, 0x19, 0xF1, 0x20, 0x75, 0x2A, 0xDD, 0x7B, 0x5C,
        0xE6, 0x17, 0xDB, 0x4B, 0x72, 0xC7, 0x83, 0x06, 0x10, 0x91, 0x70, 0x33,
        0x42, 0x0D, 0x75, 0xF9, 0xB8, 0x14, 0x39, 0x5A, 0xCF, 0xAE, 0x6A, 0xEC,
        0x7D, 0x3A, 0x2A, 0x87, 0xF8, 0x86, 0xA8, 0xEA, 0x25, 0x7E, 0xB5, 0xF9,
        0x61, 0xE8, 0xA5, 0x5E, 0x20, 0x2F, 0xA2, 0x99, 0x85, 0xA3, 0xCC, 0xCD,
        0x5C, 0x39, 0x1B, 0x6D, 0x1B, 0x17, 0xA9, 0xB4, 0xEB, 0x95, 0xDD, 0xFB,
        0xBE, 0x3C, 0x2C, 0x3B, 0xE9, 0x7D, 0x5D, 0x3E, 0x78, 0x37, 0x23, 0xDA,
        0xA5, 0x35, 0xD8, 0x36, 0xA7, 0x42, 0xD6, 0xDB, 0x38, 0xBA, 0x17, 0x12,
        0x8C, 0x76, 0x83, 0x38, 0xD8, 0x23, 0x02, 0x38, 0x26, 0xE3, 0xE7, 0xE2,
        0x5E, 0xCB, 0xC9, 0x90, 0xD2, 0x46, 0x27, 0x84, 0x77, 0x41, 0x6B, 0xB5,
        0x7A, 0x4A, 0x4F, 0x45, 0xAA, 0xAB, 0x50, 0xA7, 0x58, 0x35, 0xE8, 0xA9,
        0x27, 0xC1, 0xB8, 0xA9, 0x32, 0x03, 0x02, 0x3D, 0x19, 0x77, 0x5A, 0xD2,
        0x0C, 0x52, 0x08, 0x01, 0xFA, 0xB9, 0xB2, 0x86, 0xFD, 0x24, 0x73, 0xC3,
        0x39, 0xDE, 0x4F, 0x86, 0x93, 0x19, 0xD7, 0xD5, 0x65, 0x00, 0xF1, 0x2D,
        0x0C, 0x6F, 0x3C, 0x21, 0xD0, 0xC6, 0x27, 0x99, 0x05, 0x19, 0x7C, 0x0D,
        0x57, 0x33, 0x4F, 0x8C, 0x2F, 0x72, 0x97, 0x5A, 0xFA, 0x08, 0x51, 0x51,
        0xBC, 0x56, 0xD4, 0xC4, 0xED, 0x01, 0xEB, 0xE2, 0x6A, 0x82, 0xC6, 0x4C,
        0x09, 0x76, 0xE3, 0xFA, 0x87, 0xE2, 0xD7, 0x68, 0x13, 0xA5, 0xCF, 0x32,
        0xA2, 0x16, 0x6C, 0x53, 0x50, 0x2D, 0xD2, 0x58, 0xE4, 0x67, 0x18, 0x7B,
        0x8A, 0x84, 0xE3, 0xA4, 0x49, 0x14, 0x64, 0xD5, 0x06, 0x68, 0xC7, 0x45,
        0x68, 0xEB, 0x4A, 0xB0,
    ]
)

_PBKDF2_ITERATIONS = 4096
_KEY_SIZE = 32
_IV_SIZE = 16


class CredentialError(RuntimeError):
    """Raised when credentials cannot be found or decrypted."""


@dataclass
class Cookie:
    """Minimal representation of an http.Cookie entry."""

    name: str
    value: str

    @classmethod
    def from_dict(cls, d: dict) -> "Cookie":
        return cls(name=d.get("Name", ""), value=d.get("Value", ""))


@dataclass
class Credentials:
    token: str
    cookies: List[Cookie] = field(default_factory=list)

    def cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.cookies if c.name)

    @property
    def token_prefix(self) -> str:
        """Return only the token type, never the secret value."""
        return self.token.split("-", 1)[0] if "-" in self.token else "unknown"

    def __repr__(self) -> str:  # never leak secrets via repr()
        return (
            f"Credentials(token=<{self.token_prefix}-...>, "
            f"cookies=<{len(self.cookies)}>)"
        )


def default_cache_dir() -> Path:
    """Return slackdump's credential cache directory for this OS."""
    override = os.environ.get("SLACKDUMP_CACHE_DIR")
    if override:
        return Path(override).expanduser()

    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        return home / "Library" / "Caches" / "slackdump"
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return Path(base) / "slackdump"
    # Linux / other unixes follow the XDG cache convention.
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else home / ".cache"
    return base / "slackdump"


def current_workspace(cache_dir: Path) -> Optional[str]:
    """Return the workspace name stored in ``workspace.txt``, if present."""
    ptr = cache_dir / "workspace.txt"
    try:
        name = ptr.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return name or None


def _machine_id_macos() -> str:
    try:
        out = subprocess.check_output(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", "replace")
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        raise CredentialError(f"cannot read IOPlatformUUID: {exc}") from exc

    match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
    if not match:
        raise CredentialError("IOPlatformUUID not found in ioreg output")
    return match.group(1)


# Paths checked by denisbrodbeck/machineid, in ITS order (dbus first).
_LINUX_ID_PATHS = ("/var/lib/dbus/machine-id", "/etc/machine-id")
_CGROUP_FILE = "/proc/1/cgroup"
# Markers used by rusq/encio to decide it is running inside a container.
# NOTE: "0::/" is the cgroup-v2 hierarchy line and also appears on many
# *non-container* hosts.  We replicate this faithfully: slackdump used the
# same check when it wrote the credential file, so matching its behaviour
# (bug included) is what makes decryption work.
_CONTAINER_MARKERS = ("lxc", "docker", "0::/")


def _raw_machine_id_linux() -> str:
    """Raw machine id, mirroring machineid.machineID() on Linux.

    The library tries ``/var/lib/dbus/machine-id`` first, then
    ``/etc/machine-id``.
    """
    for candidate in _LINUX_ID_PATHS:
        try:
            value = Path(candidate).read_text(encoding="utf-8")
        except OSError:
            continue
        value = value.strip()
        if value:
            return value
    raise CredentialError(
        "could not read a machine id from " + " or ".join(_LINUX_ID_PATHS)
    )


def _in_container_linux() -> bool:
    """Mirror encio's isInContainer() using /proc/1/cgroup."""
    try:
        with open(_CGROUP_FILE, "r", encoding="utf-8", errors="replace") as handle:
            for _, line in zip(range(6), handle):  # maxlines = 5 (+1)
                if any(marker in line for marker in _CONTAINER_MARKERS):
                    return True
    except OSError:
        # encio treats a read error as "not a container".
        return False
    return False


def _gen_id_linux() -> str:
    """Mirror encio's genID(): hostname, else ``uname -n``."""
    try:
        return os.uname().nodename
    except (AttributeError, OSError):  # pragma: no cover
        pass
    try:
        return subprocess.check_output(
            ["uname", "-n"], stderr=subprocess.DEVNULL
        ).decode("utf-8", "replace")
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover
        return "no-machine-id"


def machine_id() -> str:
    """Return the *raw* machine identifier (no app-id hashing)."""
    system = platform.system()
    if system == "Darwin":
        return _machine_id_macos()
    if system == "Linux":
        return _raw_machine_id_linux()
    raise CredentialError(
        f"machine id lookup is not supported on {system!r}; "
        "set MACHINE_ID_OVERRIDE to a custom machine id manually"
    )


def _override() -> Optional[str]:
    """Return an explicit machine-id override, if the user supplied one.

    ``MACHINE_ID_OVERRIDE`` is a boolean toggle in slackdump; we additionally
    accept a real value so users can supply a known id directly.
    """
    value = os.environ.get("MACHINE_ID_OVERRIDE")
    if not value or value.lower() in {"1", "true"}:
        return None
    return value


def _protected_id(app_id: str, machine: Optional[str]) -> str:
    """Mirror machineid.ProtectedID / encio's container branch.

    * override / normal path: ``hex(HMAC-SHA256(key=machine_id, msg=appID))``
    * Linux *in a container*: ``hex(sha256(hostname + appID))``  (encio quirk)
    """
    if machine is not None:
        # idOverrideFn / ProtectedID: HMAC keyed by the machine id.
        return hmac.new(
            machine.encode("utf-8"), app_id.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    override = _override()
    if override is not None:
        return hmac.new(
            override.encode("utf-8"), app_id.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    if platform.system() == "Linux" and _in_container_linux():
        compound = _gen_id_linux().encode("utf-8") + app_id.encode("utf-8")
        return hashlib.sha256(compound).hexdigest()

    return hmac.new(
        machine_id().encode("utf-8"), app_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def derive_key(machine: Optional[str] = None) -> bytes:
    """Derive the AES-256 key from the machine id (see module docstring)."""
    protected = _protected_id(APP_ID, machine)
    return hashlib.pbkdf2_hmac(
        "sha512", protected.encode("utf-8"), _SALT, _PBKDF2_ITERATIONS, _KEY_SIZE
    )


def decrypt_file(path: Path, key: bytes) -> bytes:
    """Decrypt a slackdump credential file and return the plaintext bytes."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise CredentialError(f"cannot read credential file {path}: {exc}") from exc

    if len(data) <= _IV_SIZE:
        raise CredentialError(
            f"{path} is too short to be an encrypted slackdump credential file"
        )

    iv, ciphertext = data[:_IV_SIZE], data[_IV_SIZE:]
    decryptor = Cipher(algorithms.AES(key), CFB(iv)).decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


def parse_credentials(plaintext: bytes) -> Credentials:
    """Parse the decrypted JSON payload into a :class:`Credentials` object."""
    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialError(
            "decrypted data is not valid JSON - the key is probably wrong "
            "(e.g. credentials were copied from another machine)"
        ) from exc

    token = payload.get("Token") or payload.get("token")
    if not token:
        raise CredentialError("credential payload contains no token")

    raw_cookies = payload.get("Cookie") or payload.get("cookie") or []
    cookies = [Cookie.from_dict(c) for c in raw_cookies if isinstance(c, dict)]
    return Credentials(token=token, cookies=cookies)


def load_credentials(
    workspace: Optional[str] = None,
    cache_dir: Optional[Path] = None,
    machine: Optional[str] = None,
) -> Credentials:
    """Load and decrypt credentials for ``workspace`` (or the current one).

    Resolution order for the workspace name:

    1. the ``workspace`` argument,
    2. the contents of ``workspace.txt`` in the cache directory.
    """
    cache = Path(cache_dir) if cache_dir else default_cache_dir()
    if not cache.is_dir():
        raise CredentialError(f"slackdump cache directory not found: {cache}")

    name = workspace or current_workspace(cache)
    if not name:
        raise CredentialError(
            f"no workspace selected and {cache / 'workspace.txt'} is missing; "
            "pass --workspace"
        )

    cred_path = cache / f"{name}.bin"
    if not cred_path.is_file():
        raise CredentialError(f"credential file not found: {cred_path}")

    key = derive_key(machine)
    plaintext = decrypt_file(cred_path, key)
    return parse_credentials(plaintext)
