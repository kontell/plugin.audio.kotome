"""HTTP transport for the AudioBookShelf client, on the standard library.

This exists instead of `requests` for one measured reason: inside Kodi's
embedded Python, `import requests` costs 0.5-1.2 s, and a plugin invocation is
a fresh process that makes one to three calls and exits. The connection
pooling never gets a chance to pay for itself while the import cost is paid
every single time a user opens a folder. `http.client`, `ssl`, `json` and
`urllib` together cost 0.15-0.28 s, most of it `ssl`.

Measured on Kodi 21.3 (desktop); an ARM box pays a multiple of both numbers.

Errors are raised rather than returned so a caller can tell "your token
expired" from "the server is not there" — the two need opposite responses.
"""

import gzip
import json as _json
import ssl
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_TIMEOUT = 15


class HttpError(Exception):
    """The server answered, but not with success."""

    def __init__(self, status, body=b"", url=""):
        self.status = status
        self.body = body
        self.url = url
        super().__init__("HTTP {} for {}".format(status, url))


class Unauthorized(HttpError):
    """401/403 — the credentials are wrong, missing, or expired."""


class Unreachable(Exception):
    """No answer at all: DNS, connection refused, TLS failure, timeout."""


class Response:
    __slots__ = ("status", "body", "headers")

    def __init__(self, status, body, headers):
        self.status = status
        self.body = body
        self.headers = headers

    def json(self):
        if not self.body:
            return {}
        return _json.loads(self.body.decode("utf-8"))


class Http:
    """A small request/response transport over urllib.

    One instance per client. `headers` is the per-instance default set, which
    is where the Authorization header lives.
    """

    def __init__(self, verify=True, timeout=DEFAULT_TIMEOUT):
        self.timeout = timeout
        context = ssl.create_default_context()
        if not verify:
            # Self-hosted servers behind a self-signed certificate are common
            # enough that refusing outright is the wrong default for a setting
            # the user had to turn on deliberately.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context)
        )
        self.headers = {
            "Accept": "application/json",
            # Free on a 300 KB library listing, and the saving is largest
            # exactly where the connection is slowest.
            "Accept-Encoding": "gzip",
        }

    def request(self, method, url, params=None, json_body=None, headers=None):
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)

        body = None
        request_headers = dict(self.headers)
        if headers:
            request_headers.update(headers)
        if json_body is not None:
            body = _json.dumps(json_body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            url, data=body, headers=request_headers, method=method
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return Response(response.status, _read(response), response.headers)
        except urllib.error.HTTPError as error:
            # HTTPError is itself a response, so the body is still readable and
            # worth keeping — ABS puts its reason in there.
            payload = _read(error)
            if error.code in (401, 403):
                raise Unauthorized(error.code, payload, url) from None
            raise HttpError(error.code, payload, url) from None
        except (urllib.error.URLError, ssl.SSLError, OSError) as error:
            raise Unreachable(str(error)) from None

    def close(self):
        self._opener.close()


def _read(response):
    """Body bytes, un-gzipped if the server used it."""
    raw = response.read()
    if response.headers.get("Content-Encoding") == "gzip":
        try:
            return gzip.decompress(raw)
        except (OSError, EOFError):
            # A truncated or mislabelled body is worth returning as-is rather
            # than turning into a transport error the caller cannot act on.
            return raw
    return raw
