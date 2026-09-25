"""Server resolution and authentication for AudioBookShelf.

Dialog-free on purpose: everything here talks to the server or to stored
settings, and the keyboards and pickers live in main.py. That split is
plugin.video.kofin's, and it is what makes this the only module that could be
tested without a Kodi in front of it.

The token model is AudioBookShelf 2.26+, read out of the server source rather
than the docs (which still describe the old one):

    POST /login          x-return-tokens: true
                         -> user.accessToken   (1 hour by default)
                         -> user.refreshToken  (30 days)

    POST /auth/refresh   x-refresh-token: <refresh token>
                         -> a new access token and a rotated refresh token

`user.token` still exists and still works, but server/models/User.js annotates
it "TODO: Old non-expiring token". It is read only as a fallback for servers
older than 2.26, and never minted here.
"""

import time
import uuid
from urllib.parse import urlsplit

import xbmcaddon

from abs_http import Http, HttpError, Unauthorized, Unreachable

DEFAULT_PORT = 13378

# Refresh this long before the access token actually expires, so a request
# does not race the clock.
REFRESH_MARGIN_SECONDS = 120
# A playback session outlives a listing by hours. Start one on a token with
# at least this much life left, because the stream URL carries the token and
# a mid-book expiry is a stall the user cannot explain.
PLAYBACK_MIN_REMAINING_SECONDS = 30 * 60
# Fallback lifetime when the server does not say. Matches the ABS default.
DEFAULT_ACCESS_LIFETIME = 3600


class AuthResult:
    def __init__(self, access_token, refresh_token, user_name, user_id, expires_at):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.user_name = user_name
        self.user_id = user_id
        self.expires_at = expires_at

    @classmethod
    def from_response(cls, data, now=None):
        now = time.time() if now is None else now
        user = (data or {}).get("user") or {}
        # accessToken is the modern field; token is the deprecated
        # non-expiring one, kept so an older server still logs in.
        access = user.get("accessToken") or user.get("token") or ""
        legacy = not user.get("accessToken")
        return cls(
            access_token=access,
            refresh_token=user.get("refreshToken") or "",
            user_name=user.get("username") or user.get("displayName") or "",
            user_id=user.get("id") or "",
            # A legacy token does not expire, so nothing should try to refresh
            # it: 0 means "no expiry known".
            expires_at=0 if legacy else now + DEFAULT_ACCESS_LIFETIME,
        )


def normalize_address(text):
    """'host', 'host:port' or a full URL -> canonical base URL, no trailing /.

    A bare host gets http on ABS's default port; explicit schemes and ports
    pass through, and https without a port stays portless (i.e. 443), because
    a reverse proxy in front of ABS is the common deployment.
    """
    address = (text or "").strip().rstrip("/")
    if not address:
        return ""
    if "://" not in address:
        address = "http://" + address
    parts = urlsplit(address)
    netloc = parts.netloc
    if ":" not in netloc and parts.scheme == "http":
        netloc = "%s:%d" % (netloc, DEFAULT_PORT)
    base = "%s://%s" % (parts.scheme, netloc)
    if parts.path:
        base += parts.path.rstrip("/")
    return base


def server_status(http, address):
    """GET /status — unauthenticated, so a wrong address fails as a wrong
    address rather than as a failed login."""
    return http.request("GET", address + "/status").json()


def login(http, address, username, password):
    response = http.request(
        "POST",
        address + "/login",
        json_body={"username": username, "password": password},
        # Without this the refresh token comes back only as a Set-Cookie, and
        # a Kodi add-on is not a browser: there is no cookie jar to put it in.
        headers={"x-return-tokens": "true"},
    )
    return AuthResult.from_response(response.json())


def refresh(http, address, refresh_token):
    response = http.request(
        "POST",
        address + "/auth/refresh",
        headers={"x-refresh-token": refresh_token},
    )
    result = AuthResult.from_response(response.json())
    # The route rotates the refresh token, but only returns it when asked via
    # the header — which is what we did. Keep the old one if it did not.
    if not result.refresh_token:
        result.refresh_token = refresh_token
    return result


def logout(http, address, access_token):
    """Best effort: the local credentials are cleared either way."""
    try:
        http.request(
            "POST",
            address + "/logout",
            headers={"Authorization": "Bearer " + access_token},
        )
    except (HttpError, Unreachable):
        pass


class Credentials:
    """What is stored about the signed-in user, in hidden add-on settings.

    Settings rather than a file so it lives and dies with the Kodi profile it
    belongs to, and so the settings UI can hide the account rows until there
    is an account — a <dependency> can read a setting and cannot read a file.

    The password is never among these. It is asked for in a dialog, exchanged
    for tokens, and dropped.
    """

    FIELDS = (
        "server_url",
        "server_name",
        "user_name",
        "access_token",
        "refresh_token",
        "device_id",
    )

    def __init__(self, addon=None):
        self._addon = addon or xbmcaddon.Addon()
        for name in self.FIELDS:
            setattr(self, name, self._addon.getSetting(name) or "")
        self.expires_at = _as_float(self._addon.getSetting("token_expires_at"))
        self.logged_in = self._addon.getSetting("logged_in") == "true"
        if not self.device_id:
            # Per install, not a constant shared by every Kotome in the
            # world: ABS keys its session list on this, and a shared id makes
            # every user look like the same device.
            self.device_id = uuid.uuid4().hex
            self._addon.setSetting("device_id", self.device_id)

    def save(self):
        for name in self.FIELDS:
            self._addon.setSetting(name, getattr(self, name) or "")
        self._addon.setSetting("token_expires_at", str(self.expires_at or 0))
        self._addon.setSetting("logged_in", "true" if self.logged_in else "false")

    def clear(self):
        """Sign out. The server address and device id survive: the first is
        how you sign back in, and the second identifies this box to the
        server across sessions."""
        for name in ("server_name", "user_name", "access_token", "refresh_token"):
            setattr(self, name, "")
        self.expires_at = 0
        self.logged_in = False
        self.save()

    def apply(self, result, address="", server_name=""):
        self.access_token = result.access_token
        self.refresh_token = result.refresh_token
        self.user_name = result.user_name or self.user_name
        self.expires_at = result.expires_at
        if address:
            self.server_url = address
        if server_name:
            self.server_name = server_name
        self.logged_in = bool(result.access_token)

    # ── token lifecycle ──

    @property
    def has_credentials(self):
        return bool(self.server_url and self.access_token)

    @property
    def bearer(self):
        return self.access_token

    def needs_refresh(self, min_remaining=REFRESH_MARGIN_SECONDS):
        if not self.refresh_token or not self.expires_at:
            # No expiry known means a legacy non-expiring token: nothing to do.
            return False
        return time.time() + min_remaining >= self.expires_at

    def refresh_now(self, http, min_remaining=REFRESH_MARGIN_SECONDS):
        """Swap the refresh token for a new access token. True if it worked."""
        if not self.refresh_token:
            return False
        if not self.needs_refresh(min_remaining):
            return True
        try:
            result = refresh(http, self.server_url, self.refresh_token)
        except (Unauthorized, HttpError, Unreachable):
            return False
        if not result.access_token:
            return False
        self.apply(result)
        self.save()
        return True


def transport(verify=True):
    return Http(verify=verify)


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
