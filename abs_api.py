"""AudioBookShelf API client."""

import xbmc

from abs_auth import PLAYBACK_MIN_REMAINING_SECONDS
from abs_http import Http, HttpError, Unauthorized, Unreachable

CLIENT_NAME = "Kotome"


class ABSClient:
    """Client for the AudioBookShelf REST API."""

    def __init__(self, server_url, token=None, verify=True, auth=None):
        self.server_url = server_url.rstrip("/")
        self.token = token
        # Set by every request, so a caller can tell an expired token from an
        # unreachable server without catching anything.
        self.last_error = None
        # An abs_auth.Credentials, when there is one. Lets a 401 be answered
        # with a token refresh instead of an error the user has to read.
        self._auth = auth
        self.device_id = getattr(auth, "device_id", "") or "kodi-kotome"
        self.session = Http(verify=verify)
        self._set_token(token)

    @classmethod
    def from_credentials(cls, creds, verify=True):
        return cls(creds.server_url, token=creds.bearer, verify=verify, auth=creds)

    def _set_token(self, token):
        self.token = token
        if token:
            self.session.headers["Authorization"] = "Bearer " + token
        else:
            self.session.headers.pop("Authorization", None)

    def _request(self, method, path, params=None, json_body=None, headers=None):
        """Return the decoded body, or None after logging why not.

        Callers are listing routes that have to render something either way,
        so None is the useful answer. `last_error` carries the distinction for
        the ones that care.
        """
        self.last_error = None
        url = self.server_url + path
        try:
            response = self.session.request(
                method, url, params=params, json_body=json_body, headers=headers
            )
        except Unauthorized as error:
            # One refresh, one retry. An access token lives an hour by
            # default, so this is the ordinary path after an idle evening,
            # not an exceptional one — and the alternative is asking the user
            # to sign in again every morning.
            if self._refresh_token_now():
                try:
                    response = self.session.request(
                        method, url, params=params, json_body=json_body, headers=headers
                    )
                except (HttpError, Unreachable) as retry_error:
                    self.last_error = retry_error
                    xbmc.log(
                        "ABSClient {} {} failed after refresh: {}".format(
                            method, path, retry_error
                        ),
                        xbmc.LOGERROR,
                    )
                    return None
            else:
                self.last_error = error
                xbmc.log(
                    "ABSClient {} {} unauthorised".format(method, path),
                    xbmc.LOGWARNING,
                )
                return None
        except (HttpError, Unreachable) as error:
            self.last_error = error
            xbmc.log(
                "ABSClient {} {} failed: {}".format(method, path, error), xbmc.LOGERROR
            )
            return None
        try:
            return response.json()
        except ValueError:
            # A 204, or a success with an empty body: both mean "it worked".
            return {}

    def _refresh_token_now(self):
        """Refresh regardless of the clock: something already said 401."""
        if self._auth is None:
            return False
        if not self._auth.refresh_now(self.session, min_remaining=0):
            return False
        self._set_token(self._auth.bearer)
        xbmc.log("Kotome: access token refreshed", xbmc.LOGINFO)
        return True

    def ensure_fresh_token(self, min_remaining=None):
        """Refresh ahead of time if the token is close to expiring.

        Called before starting playback: the stream URL carries the token and
        a book outlasts an hour easily, so a token that is nearly up needs
        replacing before the session starts rather than during it.
        """
        if self._auth is None:
            return
        kwargs = {} if min_remaining is None else {"min_remaining": min_remaining}
        if self._auth.needs_refresh(**kwargs) and self._auth.refresh_now(
            self.session, **kwargs
        ):
            self._set_token(self._auth.bearer)

    def _get(self, path, params=None):
        return self._request("GET", path, params=params)

    def _post(self, path, json=None, headers=None):
        return self._request("POST", path, json_body=json or {}, headers=headers)

    def _delete(self, path):
        return self._request("DELETE", path)

    def _patch(self, path, json=None):
        return self._request("PATCH", path, json_body=json or {})

    # ── Libraries ──

    def get_libraries(self):
        data = self._get("/api/libraries")
        return data.get("libraries", []) if data else []

    def get_library(self, library_id):
        return self._get("/api/libraries/{}".format(library_id))

    # ── Library items (books / podcasts) ──

    def get_library_items(
        self, library_id, page=0, limit=50, sort=None, desc=False, filter_str=None
    ):
        params = {"page": page, "limit": limit}
        if sort:
            params["sort"] = sort
        if desc:
            params["desc"] = 1
        if filter_str:
            params["filter"] = filter_str
        return self._get("/api/libraries/{}/items".format(library_id), params=params)

    def get_item(self, item_id, expanded=True):
        params = {"expanded": 1} if expanded else {}
        return self._get("/api/items/{}".format(item_id), params=params)

    # ── Series ──

    def get_series(self, library_id, page=0, limit=50):
        return self._get(
            "/api/libraries/{}/series".format(library_id),
            params={"page": page, "limit": limit},
        )

    def get_series_detail(self, library_id, series_id):
        return self._get("/api/libraries/{}/series/{}".format(library_id, series_id))

    # ── Authors ──

    def get_authors(self, library_id):
        data = self._get("/api/libraries/{}/authors".format(library_id))
        return data.get("authors", []) if data else []

    # ── Collections ──

    def get_collections(self, library_id):
        data = self._get("/api/libraries/{}/collections".format(library_id))
        return data.get("results", []) if data else []

    # ── Search ──

    def search(self, library_id, query, limit=20):
        return self._get(
            "/api/libraries/{}/search".format(library_id),
            params={"q": query, "limit": limit},
        )

    # ── Podcast episodes ──

    def get_recent_episodes(self, library_id, limit=50):
        return self._get(
            "/api/libraries/{}/recent-episodes".format(library_id),
            params={"limit": limit},
        )

    # ── Continue listening ──

    def get_items_in_progress(self):
        data = self._get("/api/me/items-in-progress")
        return data.get("libraryItems", []) if data else []

    def get_all_progress(self):
        """Fetch /api/me and return a dict of progress keyed by libraryItemId.
        For podcast episodes, key is 'libraryItemId-episodeId'."""
        data = self._get("/api/me")
        if not data:
            return {}
        progress = {}
        for p in data.get("mediaProgress", []):
            item_id = p.get("libraryItemId", "")
            ep_id = p.get("episodeId")
            if ep_id:
                progress["{}-{}".format(item_id, ep_id)] = p
            else:
                progress[item_id] = p
        return progress

    # ── Playback sessions ──

    def start_playback(self, item_id, episode_id=None, use_hls=False):
        """Create a playback session. Direct play by default, HLS if use_hls=True."""
        # The stream URL carries the token, and a book outlasts the one-hour
        # access token easily. Start the session on a fresh one.
        self.ensure_fresh_token(PLAYBACK_MIN_REMAINING_SECONDS)
        path = "/api/items/{}/play".format(item_id)
        if episode_id:
            path = "/api/items/{}/play/{}".format(item_id, episode_id)
        body = {
            "deviceInfo": {
                "clientName": CLIENT_NAME,
                "deviceId": self.device_id,
            },
        }
        if not use_hls:
            body["forceDirectPlay"] = True
        else:
            body["forceTranscode"] = True
        return self._post(path, json=body)

    def sync_session(self, session_id, current_time, duration, time_listened):
        return self._post(
            "/api/session/{}/sync".format(session_id),
            json={
                "currentTime": current_time,
                "duration": duration,
                "timeListened": time_listened,
            },
        )

    def close_session(self, session_id):
        return self._post("/api/session/{}/close".format(session_id))

    # ── Progress ──

    def get_progress(self, item_id, episode_id=None):
        path = "/api/me/progress/{}".format(item_id)
        if episode_id:
            path += "/" + episode_id
        return self._get(path)

    def update_progress(
        self, item_id, current_time, duration, is_finished=False, episode_id=None
    ):
        path = "/api/me/progress/{}".format(item_id)
        if episode_id:
            path += "/" + episode_id
        progress = current_time / duration if duration > 0 else 0
        return self._patch(
            path,
            json={
                "currentTime": current_time,
                "progress": progress,
                "isFinished": is_finished,
            },
        )

    def set_finished(self, item_id, episode_id=None, finished=True):
        """Mark an item played or unplayed on the server."""
        path = "/api/me/progress/{}".format(item_id)
        if episode_id:
            path += "/" + episode_id
        return self._patch(path, json={"isFinished": bool(finished)})

    def clear_progress(self, progress_id):
        """Forget a resume position entirely, server-side.

        Takes the mediaProgress record's own id, not the library item's.
        ApiRouter registers this as delete('/me/progress/:id') with no
        episode segment — unlike the GET and PATCH beside it, which are
        ':libraryItemId/:episodeId?'. Passing an item/episode pair here does
        not match the route.
        """
        return self._delete("/api/me/progress/{}".format(progress_id))

    # ── URLs ──

    def cover_url(self, item_id):
        return "{}/api/items/{}/cover".format(self.server_url, item_id)

    def author_image_url(self, author_id):
        return "{}/api/authors/{}/image".format(self.server_url, author_id)

    def stream_url(self, content_url):
        """Turn a relative content URL from a play session into an absolute URL."""
        if content_url.startswith("http"):
            return content_url
        url = self.server_url + content_url
        if self.token and "?" not in url:
            url += "?token=" + self.token
        elif self.token:
            url += "&token=" + self.token
        return url
