"""Kotome - AudioBookShelf client for Kodi."""

import html
import os
import re
import sys
import json
import time
from html.parser import HTMLParser
from base64 import b64encode
from urllib.parse import urlencode, parse_qs, urlsplit

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

import abs_auth
import abs_discovery
import abs_http
from abs_api import ABSClient

# ── Plugin bootstrap ──

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
PROFILE_DIR = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
# Re-read per invocation, not frozen at import: with reuselanguageinvoker the
# module outlives the invocation that loaded it, so anything derived from
# sys.argv is stale on every call after the first. Stale here would mean
# writing a listing to a handle that closed minutes ago.
HANDLE = -1
BASE_URL = ""


def _refresh_invocation():
    global HANDLE, BASE_URL
    try:
        HANDLE = int(sys.argv[1])
    except (IndexError, ValueError):
        HANDLE = -1
    BASE_URL = sys.argv[0]


# ssl.create_default_context() loads the system CA bundle, which is not free,
# and get_client() ran once per invocation. Keyed on what identifies a client,
# so a re-login makes a new one rather than reusing a stale token.
_client_cache = {"key": None, "client": None}


def _verify_ssl():
    return ADDON.getSetting("ssl_verify") != "false"


def get_client():
    """The API client for the signed-in user, or None if there is not one.

    No round trip to check the token: that used to be a get_libraries() call
    on every listing whose result was thrown away. An expired token announces
    itself on the next real request, and ABSClient answers it with a refresh.

    Does not prompt. A missing account is the root with a Settings row, not
    a dialog the user did not ask for — widgets and library nodes hit this
    path too, and a modal there is unprompted.
    """
    creds = abs_auth.Credentials(ADDON)
    if not creds.has_credentials:
        return None

    key = (creds.server_url, creds.bearer)
    if _client_cache["key"] == key and _client_cache["client"] is not None:
        return _client_cache["client"]
    client = ABSClient.from_credentials(creds, verify=_verify_ssl())
    _client_cache["key"] = key
    _client_cache["client"] = client
    return client


# Every listing route needs the progress map, and it arrives as the whole
# /api/me document — 59 KB on a modest account. Under interpreter reuse the
# parsed map outlives the invocation, so the second folder in a row is free.
# Short TTL because the service writes progress back as playback runs.
_PROGRESS_TTL = 30
_progress_cache = {"at": 0.0, "map": None}


def get_progress_map(client):
    now = time.time()
    if (
        _progress_cache["map"] is not None
        and now - _progress_cache["at"] < _PROGRESS_TTL
    ):
        return _progress_cache["map"]
    progress = client.get_all_progress()
    _progress_cache["at"] = now
    _progress_cache["map"] = progress
    return progress


def invalidate_progress_cache():
    _progress_cache["map"] = None
    _author_index["by_name"] = {}
    _author_index["by_library"] = {}
    _author_index["at"] = 0.0


def build_url(**kwargs):
    """Build a plugin:// URL from keyword arguments."""
    for k, v in kwargs.items():
        if isinstance(v, (dict, list)):
            kwargs[k] = json.dumps(v)
    return "{}?{}".format(BASE_URL, urlencode(kwargs))


ICON_DIR = os.path.join(
    xbmcvfs.translatePath(ADDON.getAddonInfo("path")), "resources", "icons"
)
# Material Symbols (Apache-2.0), for what Kodi has no icon for. Regenerate
# with tools/make-icons.py.
ICON_BOOKS = os.path.join(ICON_DIR, "books.png")
ICON_CONTINUE = os.path.join(ICON_DIR, "continue.png")
ICON_SORT = os.path.join(ICON_DIR, "sort.png")
ICON_NEXT = os.path.join(ICON_DIR, "navigate_next.png")

# The add-on's own backdrop. Declared in addon.xml <assets> too, which is what
# the add-on browser shows — but a skin draws the background of a *listing*
# from the focused item's art, so menu rows have to carry it themselves or
# there is simply nothing there.
FANART = os.path.join(
    xbmcvfs.translatePath(ADDON.getAddonInfo("path")), "resources", "fanart.jpg"
)

# Shown for an item whose cover the server does not have.
FALLBACK_COVER = os.path.join(
    xbmcvfs.translatePath(ADDON.getAddonInfo("path")), "resources", "ABS-Default.png"
)

# Kodi resolves a bare "DefaultX.png" out of whichever skin is running, so
# these match the user's theme instead of fighting it and nothing needs to be
# shipped. All of them were checked against a live skin before being used
# here; two obvious-looking names were rejected in the process:
# DefaultAddonAudio.png draws a clapperboard (it is the add-on *category*
# icon) and DefaultTVShows.png draws a television, which is not a book series.
#
# Only routes with no Kodi equivalent fall back to a bundled Material Symbol.
_ACTION_ICONS = {
    "continue_listening": ICON_CONTINUE,
    # Books get the Material "auto_stories" open book: Kodi's nearest names
    # are all music ones (DefaultMusicAlbums.png is a disc), and a book
    # library is the one thing in this add-on that deserves a real book.
    "library": ICON_BOOKS,
    "library_items": ICON_BOOKS,
    "podcast_items": "DefaultAddonLyrics.png",
    "series_list": "DefaultSets.png",
    "series_detail": "DefaultSets.png",
    "authors_list": "DefaultMusicArtists.png",
    "author_books": "DefaultMusicArtists.png",
    "collections_list": "DefaultMusicPlaylists.png",
    "collection_detail": "DefaultMusicPlaylists.png",
    "podcast_episodes": "DefaultAddonLyrics.png",
    "recent_episodes": "DefaultRecentlyAddedEpisodes.png",
    "recently_added": "DefaultMusicRecentlyAdded.png",
    "search": "DefaultMusicSearch.png",
    "settings": "DefaultAddonService.png",
    "speed_dialog": "DefaultMusicSongs.png",
    "set_sleep_timer": "DefaultAddonScreensaver.png",
}


def _cover_art(client, item, item_id=None, artist=None):
    """Artwork for a library item, falling back to the bundled placeholder.

    cover_url() returns a URL whether or not the server has a cover, so an
    item without one used to render as a broken image. ABS reports what it
    has in coverPath. ``artist`` is the author's image for the info dialog
    (ListItem.Art(artist) and the cast strip); omit it when there is none.
    """
    item_id = item_id or item.get("id") or item.get("libraryItemId")
    has_cover = bool(item.get("media", {}).get("coverPath") or item.get("coverPath"))
    cover = client.cover_url(item_id) if has_cover and item_id else FALLBACK_COVER
    art = {"thumb": cover, "poster": cover, "icon": cover, "fanart": cover}
    if artist:
        art["artist"] = artist
    return art


# Library-item listings carry authorName but not author ids, so the info
# dialog had a cast row with no photo. /authors has the id and imagePath;
# look up by name. TTL matches the progress map: the listing is short-lived
# and a second folder in the same library is free.
_AUTHOR_TTL = 300
_author_index = {"at": 0.0, "by_name": {}, "by_library": {}}


def get_author_index(client, library_id=None):
    """lowercased name -> (id, image_url). image_url is empty without imagePath."""
    now = time.time()
    if _author_index["by_name"] and now - _author_index["at"] < _AUTHOR_TTL:
        if library_id:
            return (
                _author_index["by_library"].get(library_id) or _author_index["by_name"]
            )
        return _author_index["by_name"]

    by_name = {}
    by_library = {}
    libraries = client.get_libraries()
    for lib in libraries:
        if lib.get("mediaType") != "book":
            continue
        lid = lib["id"]
        index = {}
        for author in client.get_authors(lid):
            name = (author.get("name") or "").strip()
            author_id = author.get("id") or ""
            if not name or not author_id:
                continue
            thumb = (
                client.author_image_url(author_id) if author.get("imagePath") else ""
            )
            rec = (author_id, thumb)
            index[name.lower()] = rec
            by_name[name.lower()] = rec
        by_library[lid] = index
    _author_index["by_name"] = by_name
    _author_index["by_library"] = by_library
    _author_index["at"] = now
    if library_id:
        return by_library.get(library_id) or by_name
    return by_name


def _author_names(meta):
    """Author names on a library item, split if ABS concatenated them."""
    names = []
    seen = set()
    for raw in meta.get("authors") or []:
        if isinstance(raw, dict):
            name = (raw.get("name") or "").strip()
            author_id = raw.get("id") or ""
        else:
            name = str(raw).strip() if raw else ""
            author_id = ""
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append((name, author_id))
    if names:
        return names
    fallback = (meta.get("authorName") or meta.get("author") or "").strip()
    if not fallback:
        return []
    # ABS joins multiple authors with ", ". A single name can contain a
    # comma (rare); the author index lookup is what recovers the photo, so
    # a miss here is a missing picture rather than a wrong one.
    for part in fallback.split(", "):
        name = part.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append((name, ""))
    return names


def _author_entries(client, meta, library_id=None):
    """``[(name, image_url), ...]`` for the info dialog's artist/cast slots.

    Book listings do not include author ids. The photo comes from the
    library's author index, keyed on name. DialogVideoInfo (which is what
    the i button opens for these musicvideo items) draws Container(50) from
    setCast thumbnails — ListItem.Art(artist) alone is not a slot there.
    """
    index = get_author_index(client, library_id)
    entries = []
    for name, author_id in _author_names(meta):
        thumb = ""
        hit = index.get(name.lower())
        if hit:
            author_id = author_id or hit[0]
            thumb = hit[1]
        if not thumb and author_id:
            thumb = client.author_image_url(author_id)
        entries.append((name, thumb))
    return entries


def _first_artist_art(authors):
    for _name, thumb in authors:
        if thumb:
            return thumb
    return ""


# Runtime fallbacks for new string ids: Kodi's add-on string cache does not
# pick those up until Kodi itself restarts, so getLocalizedString can return
# empty after a disable/enable bounce. Settings labels have the same trap;
# these are the strings that also appear in dialogs this process draws.
_STRINGS = {
    30187: "Searching for AudioBookShelf servers…",
    30188: "Select a server",
    30189: "No AudioBookShelf servers answered — enter the address manually",
    30192: "Restart Kodi to apply this",
}


def _localize(string_id):
    text = (ADDON.getLocalizedString(string_id) or "").strip()
    return text or _STRINGS.get(string_id, "")


def set_icon(li, icon):
    """Set both icon and thumb: views differ in which one they draw."""
    art = {"fanart": FANART}
    if icon:
        art["icon"] = icon
        art["thumb"] = icon
    li.setArt(art)


def add_directory(label, icon=None, **kwargs):
    """Add a navigable folder item."""
    url = build_url(**kwargs)
    li = xbmcgui.ListItem(label)
    li.setIsFolder(True)
    set_icon(li, icon or _ACTION_ICONS.get(kwargs.get("action", "")))
    xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)


class _HTMLTextExtractor(HTMLParser):
    """HTML → plain text, preserving paragraph breaks."""

    # Tags that introduce a visual break — replaced by a newline.
    _BREAKING = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__()
        self._parts = []

    def handle_data(self, data):
        self._parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in self._BREAKING:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._BREAKING:
            self._parts.append("\n")

    def get_text(self):
        return "".join(self._parts)


def _sanitize_description(text):
    """Strip HTML tags and normalise whitespace for Kodi's info dialog.

    AudioBookShelf descriptions often arrive with HTML markup (<p>, <b>,
    <i>, &amp;, etc.) copied from Audible metadata. Kodi renders the
    comment verbatim, so the tags show through. Parse → plain text with
    paragraph breaks preserved, collapse runs of blank lines.
    """
    if not text:
        return ""
    try:
        parser = _HTMLTextExtractor()
        parser.feed(text)
        parser.close()
        plain = parser.get_text()
    except Exception:
        plain = text
    # Any entities missed by the parser (feed-level) get unescaped here.
    plain = html.unescape(plain)
    # html.unescape converts &nbsp; to U+00A0 — keep whitespace ASCII so
    # Kodi's layout engine treats it as a normal space.
    plain = plain.replace(" ", " ")
    # Collapse whitespace: trim each line, drop empty runs > 1 line.
    lines = [line.strip() for line in plain.splitlines()]
    out = []
    prev_blank = True  # avoid leading blank
    for line in lines:
        if not line:
            if prev_blank:
                continue
            prev_blank = True
        else:
            prev_blank = False
        out.append(line)
    # Collapse runs of spaces within remaining lines (tags sometimes eat
    # the single whitespace between words).
    return re.sub(r" +", " ", "\n".join(out)).strip()


def _epoch_to_str(ms_or_s):
    """ABS timestamps are usually epoch ms — convert to 'YYYY-MM-DD HH:MM:SS'."""
    if not ms_or_s:
        return ""
    try:
        val = float(ms_or_s)
        if val > 1e12:  # ms
            val /= 1000.0
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(val))
    except (ValueError, TypeError, OSError):
        return ""


def add_playable(label, url, art=None, info=None, progress=None, context=None):
    """Add a playable audio item.

    Facts go in the info tag, not in the label. Which of the two a skin draws
    varies — Contuary's list view renders ListItem.Title and ignores the label
    entirely, so the narrator and duration that used to be formatted into the
    label were invisible there, while the duration passed as a tag got its own
    right-aligned column for free.
    """
    li = xbmcgui.ListItem(label)
    li.setIsFolder(False)
    li.setProperty("IsPlayable", "true")
    if art:
        li.setArt(art)
    if context:
        li.addContextMenuItems(context)
    if info:
        tag = li.getVideoInfoTag()
        tag.setMediaType("musicvideo")
        if info.get("title"):
            tag.setTitle(info["title"])
        # DialogVideoInfo for musicvideo fills Container(50) from m_artist
        # via the music library, then skips setCast when the names match.
        # Author photos therefore have to travel as cast thumbnails, and
        # setArtists must not repeat those names. ListItem.Artist is empty
        # on rows that have a photo; the name is on the cast item instead.
        cast_items = [
            (name, thumb or "") for name, thumb in (info.get("cast") or []) if name
        ]
        has_cast_art = any(thumb for _name, thumb in cast_items)
        if info.get("artist") and not has_cast_art:
            tag.setArtists([info["artist"]])
        if cast_items:
            tag.setCast(
                [
                    xbmc.Actor(name, "Author", i, thumb)
                    for i, (name, thumb) in enumerate(cast_items)
                ]
            )
        if info.get("album"):
            tag.setAlbum(info["album"])
        if info.get("duration"):
            tag.setDuration(int(info["duration"]))
        if info.get("description"):
            tag.setPlot(info["description"])
        if info.get("narrator"):
            # Narrator is to an audiobook what a writer is to an episode, and
            # it is the field skins already have a place for.
            tag.setWriters([info["narrator"]])
        if info.get("genres"):
            tag.setGenres(info["genres"])
        year = _year_of(info.get("year"))
        if year:
            tag.setYear(year)
        last = _epoch_to_str(info.get("last_played"))
        if last:
            tag.setLastPlayed(last)
        _set_resume(tag, info, progress)
    xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)


def _year_of(value):
    """ABS publishedYear is not always a year.

    It is whatever the metadata source put there — "2024", "2024-12-17", and
    empty are all real. Take the first four digits or give up; setYear() on a
    date string raises and takes the whole listing down with it.
    """
    if not value:
        return None
    match = re.match(r"\s*(\d{4})", str(value))
    return int(match.group(1)) if match else None


def _set_resume(tag, info, progress):
    """Give Kodi a real resume point rather than writing "[42%]" in the title.

    The old label prefix sorted before every plain title (a bracket sorts
    before A), which is why Continue Listening had to be left server-ordered
    and the library used a suffix instead. A resume point costs none of that:
    skins draw their own in-progress indicator from it, and Kodi offers
    Resume / Start from beginning without being asked.
    """
    if not progress:
        return
    if progress.get("isFinished"):
        tag.setPlaycount(1)
        return
    total = float(info.get("duration") or 0)
    current = float(progress.get("currentTime") or 0)
    if total > 0 and 0 < current < total:
        tag.setResumePoint(current, total)


# Server-side sort options for the ABS library-items endpoint. Each entry
# is (display label, ABS sort key, desc, media_type restriction). Kodi's
# own SORT_METHOD_* only reorders the current page; to sort across pages
# we ask ABS for a pre-sorted page.
# Sort keys AudioBookShelf actually honours, from getOrder() in
# server/utils/queries/libraryItemsBookFilters.js. Anything not in that
# function's if/else chain falls through to `return []` — no ORDER BY at all —
# so the server answers 200 with rows in whatever order the database felt like
# and nothing anywhere says the sort was ignored.
#
# That is not hypothetical: media.metadata.titleIgnorePrefix was the default
# here and is not a key. "Title (A-Z)" returned a jumble. Prefix handling is
# the server's own sortingIgnorePrefix setting applied inside getTitleOrder(),
# not something a client selects.
#
# Verified against a live 2.36.0 by comparing each key's first page against a
# deliberate nonsense key; anything matching the nonsense ordering was being
# ignored. media.metadata.narratorName, media.metadata.seriesName and
# updatedAt all failed that check and are gone.
#
# Each entry is (display label, ABS sort key, desc, media_type restriction).
_SORT_OPTIONS = (
    ("Title (A-Z)", "media.metadata.title", False, "both"),
    ("Title (Z-A)", "media.metadata.title", True, "both"),
    ("Author (A-Z)", "media.metadata.authorNameLF", False, "book"),
    ("Author (Z-A)", "media.metadata.authorNameLF", True, "book"),
    ("Recently listened", "progress", True, "both"),
    ("Recently added", "addedAt", True, "both"),
    ("Oldest added", "addedAt", False, "both"),
    ("Recently changed on disk", "mtimeMs", True, "both"),
    ("Duration (shortest)", "media.duration", False, "book"),
    ("Duration (longest)", "media.duration", True, "book"),
    ("Published year (new)", "media.metadata.publishedYear", True, "book"),
    ("Published year (old)", "media.metadata.publishedYear", False, "book"),
    ("Size (largest)", "size", True, "both"),
    ("Random", "random", False, "both"),
)
_DEFAULT_SORT = "media.metadata.title"

# ABS filters are "<group>.<base64 of the value>". progress/not-finished is
# the complement of progress/finished — checked against a live 2.36.0, where
# the two totalled the unfiltered count exactly (98 + 47 = 145).
NOT_FINISHED_FILTER = "progress." + b64encode(b"not-finished").decode()


def _sort_label(sort_key, desc):
    for label, key, d, _ in _SORT_OPTIONS:
        if key == sort_key and d == desc:
            return label
    return ""


# One sorting system, not two. These listings arrive already sorted by the
# server, across every page — which is the only place a 137-book library can
# be sorted correctly, since Kodi's own methods only reorder the page in hand.
#
# Registering anything else here silently overrode the picker: with
# SORT_METHOD_TITLE first, asking for "Recently added" returned the right
# page and then Kodi re-sorted it alphabetically, and Container.SortMethod
# read "Title" while the sort menu said otherwise.
_SERVER_SORTED = (xbmcplugin.SORT_METHOD_UNSORTED,)

# Client-side sorting is fine where the whole set is already in hand.
_EPISODE_SORTS = (
    xbmcplugin.SORT_METHOD_UNSORTED,
    xbmcplugin.SORT_METHOD_TITLE,
    xbmcplugin.SORT_METHOD_DURATION,
)

_NAME_SORTS = (
    xbmcplugin.SORT_METHOD_UNSORTED,
    xbmcplugin.SORT_METHOD_LABEL,
)


# A menu of folders is not a content type. setContent(handle, "files") hands
# the listing to the skin's file view, which draws its own folder icon and
# ignores the one the item carries — every row came out as the same grey
# folder. An empty content type leaves the art alone, which is what
# plugin.video.kofin does for the same reason.
CONTENT_MENU = ""


# Media listings stay on a music content type. Switching to "musicvideos" was
# tried to get Kodi's watched overlay, which it derives from playcount for
# video content — it does not work here. Kodi synthesises that overlay from
# its own library, and these items are not in it: measured with
# content="musicvideos" on a finished episode, ListItem.PlayCount read "1"
# and ListItem.Overlay was still empty. The change also added Kodi's own
# "Mark as watched" to the context menu, which writes to Kodi's database
# rather than to AudioBookShelf and so sat next to ours saying the same thing
# and meaning something else.
#
# playcount is still set: it is true, it costs nothing, and a skin that reads
# ListItem.PlayCount can use it. The resume point is what Kodi does render
# for a plugin item, and that works.


def _apply_sorts(methods, content="albums"):
    """Set content type and register the listed sort methods (first = default)."""
    xbmcplugin.setContent(HANDLE, content)
    for m in methods:
        xbmcplugin.addSortMethod(HANDLE, m)


# ── Route handlers ──


def route_root(client):
    """Root menu: Continue Listening + libraries + settings."""
    # Now playing + Sleep timer — only shown when tempo is the active
    # inputstream (i.e. something Kotome is playing). Same gating, so the
    # row only appears when there's something to act on.
    if os.path.exists(ACTIVE_FILE):
        win = xbmcgui.Window(10000)
        title = win.getProperty("Kotome.NowPlaying.Title") or "current track"
        speed = win.getProperty("InputstreamTempo.SpeedDisplay") or "1.0x"
        label = "[COLOR orange]{}[/COLOR] [B]Now playing[/B]: {}".format(speed, title)
        add_directory(label, action="speed_dialog")

        sleep_remaining = win.getProperty("Kotome.SleepTimerRemaining")
        if sleep_remaining:
            sleep_label = "[COLOR orange]{}[/COLOR] [B]Sleep timer[/B]".format(
                sleep_remaining
            )
        else:
            sleep_label = "[B]Sleep timer[/B]"
        add_directory(sleep_label, action="set_sleep_timer")

    # Continue Listening
    add_directory("[B]Continue Listening[/B]", action="continue_listening")

    # Libraries at root level
    libraries = client.get_libraries()
    for lib in libraries:
        media_type = lib.get("mediaType", "book")
        add_directory(
            lib["name"],
            icon=_ACTION_ICONS[
                "podcast_items" if media_type == "podcast" else "library"
            ],
            action="library",
            library_id=lib["id"],
            media_type=media_type,
        )

    # Settings
    add_directory("[COLOR gray]Settings[/COLOR]", action="settings")

    # Without these the root inherits whatever sort the view last remembered
    # — it was displaying "Sort by: Date" on a menu that has no dates, and
    # reordering itself accordingly.
    _apply_sorts((xbmcplugin.SORT_METHOD_UNSORTED,), content=CONTENT_MENU)

    # Don't cache the root so the "Now playing" row appears/disappears
    # correctly when the user returns from playback.
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def _format_speed(speed):
    return "{:.2f}x".format(speed)


def route_speed_dialog():
    """Show a speed picker and write the result to TEMPO_FILE.
    Duplicates inputstream.tempo/speed.py dialog logic so the root menu entry
    works without RunScript plumbing."""
    if os.path.exists(ACTIVE_FILE):
        step, lo, hi = _speed_config()
        try:
            with open(TEMPO_FILE) as f:
                current = float(f.read().strip())
        except (IOError, ValueError):
            current = 1.0
        count = int(round((hi - lo) / step))
        values = [round(lo + i * step, 2) for i in range(count + 1)]
        labels = [_format_speed(v) for v in values]
        idx = min(range(len(values)), key=lambda i: abs(values[i] - current))
        sel = xbmcgui.Dialog().select("Playback speed", labels, preselect=idx)
        if sel >= 0 and abs(values[sel] - current) > 0.001:
            new_speed = values[sel]
            _write_tempo(new_speed)
            win = xbmcgui.Window(10000)
            win.setProperty("InputstreamTempo.Speed", str(new_speed))
            win.setProperty("InputstreamTempo.SpeedDisplay", _format_speed(new_speed))
            xbmc.executebuiltin(
                "Notification(Playback Speed, {}, 1200)".format(
                    _format_speed(new_speed)
                )
            )
    # Don't change the directory view — stay at root.
    xbmcplugin.endOfDirectory(
        HANDLE, succeeded=False, updateListing=False, cacheToDisc=False
    )


_SLEEP_PRESETS = (5, 10, 15, 30, 45, 60, 90)


def _end_of_chapter_minutes():
    """Wall-clock minutes until the current chapter ends, or None if no
    chapter information is available. Adjusts for the current playback
    tempo so a 10-minute chapter at 1.5× is reported as ~6.7 minutes."""
    try:
        with open(SESSION_FILE) as f:
            session = json.load(f)
    except Exception:
        return None
    chapters = session.get("chapters", [])
    if not chapters:
        return None
    try:
        current = xbmc.Player().getTime()
    except Exception:
        return None
    try:
        with open(TEMPO_FILE) as f:
            tempo = float(f.read().strip())
    except (IOError, ValueError):
        tempo = 1.0
    if tempo <= 0:
        tempo = 1.0
    for ch in chapters:
        if ch.get("start", 0) <= current < ch.get("end", 0):
            audio_remaining = ch["end"] - current
            return (audio_remaining / tempo) / 60.0
    return None


def _arm_sleep_timer(minutes):
    """Write SLEEP_FILE with end_time = now + minutes*60. The service polls
    for the file and takes over."""
    end_time = time.time() + minutes * 60
    os.makedirs(PROFILE_DIR, exist_ok=True)
    with open(SLEEP_FILE, "w") as f:
        f.write(str(end_time))


def route_set_sleep_timer():
    """Show the sleep-timer dialog. Mirrors speed_dialog: added as a
    directory item but ends with succeeded=False so the listing is not
    replaced."""
    if not os.path.exists(ACTIVE_FILE):
        xbmcplugin.endOfDirectory(
            HANDLE, succeeded=False, updateListing=False, cacheToDisc=False
        )
        return

    try:
        last = int(ADDON.getSetting("sleep_last_preset") or 30)
    except (ValueError, TypeError):
        last = 30

    timer_active = os.path.exists(SLEEP_FILE)
    options = []
    actions = []  # parallel list: 'cancel' | int minutes | 'eoc' | 'custom'
    preselect = -1
    if timer_active:
        options.append("Cancel timer")
        actions.append("cancel")

    # If last-used is a custom value not in presets, insert it
    last_is_custom = last not in _SLEEP_PRESETS and last > 0
    if last_is_custom:
        options.append("{} minutes (last used)".format(last))
        actions.append(last)
        preselect = len(options) - 1

    for m in _SLEEP_PRESETS:
        if m == last and not last_is_custom:
            options.append("{} minutes (last used)".format(m))
            preselect = len(options) - 1
        else:
            options.append("{} minutes".format(m))
        actions.append(m)

    options.append("End of chapter")
    actions.append("eoc")
    options.append("Custom...")
    actions.append("custom")

    sel = xbmcgui.Dialog().select("Sleep timer", options, preselect=max(0, preselect))
    if sel < 0:
        xbmcplugin.endOfDirectory(
            HANDLE, succeeded=False, updateListing=False, cacheToDisc=False
        )
        return

    choice = actions[sel]

    if choice == "cancel":
        try:
            os.remove(SLEEP_FILE)
        except OSError:
            pass
        xbmc.executebuiltin("Notification(Sleep timer, Cancelled, 1500)")
    elif choice == "eoc":
        mins = _end_of_chapter_minutes()
        if not mins or mins <= 0:
            xbmcgui.Dialog().ok("Sleep timer", "No chapter information available.")
        else:
            _arm_sleep_timer(mins)
            xbmc.executebuiltin(
                "Notification(Sleep timer, "
                "End of chapter ({:.0f} min), 1500)".format(mins)
            )
    elif choice == "custom":
        result = xbmcgui.Dialog().numeric(0, "Sleep timer minutes")
        if result:
            try:
                mins = int(result)
            except ValueError:
                mins = 0
            if mins > 0:
                _arm_sleep_timer(mins)
                ADDON.setSetting("sleep_last_preset", str(mins))
                xbmc.executebuiltin(
                    "Notification(Sleep timer, Set for {} min, 1500)".format(mins)
                )
    else:
        # Numeric preset
        _arm_sleep_timer(choice)
        ADDON.setSetting("sleep_last_preset", str(int(choice)))
        xbmc.executebuiltin(
            "Notification(Sleep timer, Set for {} min, 1500)".format(choice)
        )

    xbmcplugin.endOfDirectory(
        HANDLE, succeeded=False, updateListing=False, cacheToDisc=False
    )


def route_settings():
    """Open the add-on settings dialog.

    The handle is closed first, deliberately: openSettings() is modal, and
    whoever asked for this directory — a favourite, a library node, a widget —
    is blocked inside GetDirectory until the handle closes. Closing afterwards
    means blocking them for as long as the dialog is open.
    """
    xbmcplugin.endOfDirectory(
        HANDLE, succeeded=False, updateListing=False, cacheToDisc=False
    )
    ADDON.openSettings()

    # After settings close, refresh the config file for inputstream.tempo.
    _write_config_file()


# ── Account ──


def _notify(message, seconds=4):
    xbmcgui.Dialog().notification("Kotome", message, time=seconds * 1000)


def route_root_signed_out():
    """Root when there is no account: Settings, no dialog."""
    add_directory("[COLOR gray]Settings[/COLOR]", action="settings")
    _apply_sorts((xbmcplugin.SORT_METHOD_UNSORTED,), content=CONTENT_MENU)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def route_find_servers():
    """Find AudioBookShelf servers on the local network and fill in the URL.

    Reached from the Settings button with ``<close>true</close>``, so the
    settings dialog is already shut. Written into an open dialog the value
    is reverted when the user backs out. The route reopens settings
    afterwards, so the filled field is the confirmation.
    """
    creds = abs_auth.Credentials(ADDON)
    if creds.logged_in:
        return

    progress = xbmcgui.DialogProgress()
    progress.create("Kotome", _localize(30187))
    monitor = xbmc.Monitor()
    cancelled = []

    def should_cancel():
        if cancelled:
            return True
        if progress.iscanceled() or monitor.abortRequested():
            cancelled.append(True)
            return True
        return False

    def on_progress(done, total):
        if total:
            progress.update(min(99, int(done * 100 / total)), _localize(30187))

    try:
        open_hosts = abs_discovery.find_open(
            abs_discovery.lan_hosts(),
            should_cancel=should_cancel,
            on_progress=on_progress,
        )
    finally:
        progress.close()

    if cancelled:
        return

    http = abs_http.Http(verify=_verify_ssl(), timeout=2)
    candidates = []
    try:
        for host in open_hosts:
            address = "http://{}:{}".format(host, abs_discovery.DEFAULT_PORT)
            try:
                status = abs_auth.server_status(http, address)
            except (abs_http.HttpError, abs_http.Unreachable, ValueError):
                continue
            if abs_discovery.is_audiobookshelf(status):
                candidates.append((address, status))
    finally:
        http.close()

    if not candidates:
        # Deliberately not "try again": a host that is up answered the SYN
        # in milliseconds, and the usual causes (another subnet, ABS bound
        # to localhost, a reverse-proxied hostname) no retry reaches.
        _notify(_localize(30189), seconds=6)
        return

    items = []
    for address, status in candidates:
        label, detail = abs_discovery.label_for(address, status)
        items.append(xbmcgui.ListItem(label, detail))
    choice = xbmcgui.Dialog().select(_localize(30188), items, useDetails=True)
    if choice < 0:
        return

    address, _status = candidates[choice]
    ADDON.setSetting("server_url", address)
    xbmc.log("Kotome: discovery set server URL to {}".format(address), xbmc.LOGINFO)
    xbmc.executebuiltin("Addon.OpenSettings({})".format(ADDON_ID))


def route_login():
    """Sign in. Reached from the Settings button, so there is no handle.

    The password is asked for here and never stored: it is exchanged for an
    access token and a refresh token, and those are what get saved.
    """
    creds = abs_auth.Credentials(ADDON)
    if creds.logged_in:
        _notify("Already signed in as {}".format(creds.user_name or "this user"))
        return

    raw = creds.server_url or xbmcgui.Dialog().input("AudioBookShelf server address")
    if not raw:
        return
    address = abs_auth.normalize_address(raw)
    http = abs_auth.transport(verify=_verify_ssl())

    # Ask the server who it is before asking the user for a password: a typo
    # in the address should read as "cannot reach that server", not as a
    # rejected login.
    try:
        status = abs_auth.server_status(http, address)
    except (abs_http.HttpError, abs_http.Unreachable) as error:
        xbmc.log(
            "Kotome: server probe failed for {}: {}".format(address, error),
            xbmc.LOGWARNING,
        )
        xbmcgui.Dialog().ok(
            "Kotome",
            "Could not reach a server at\n{}\n\nCheck the address and that "
            "the server is running.".format(address),
        )
        return
    if status.get("app") != "audiobookshelf":
        xbmcgui.Dialog().ok(
            "Kotome",
            "Something answered at\n{}\nbut it is not an AudioBookShelf "
            "server.".format(address),
        )
        return
    server_name = "AudioBookShelf {}".format(status.get("serverVersion", ""))

    username = xbmcgui.Dialog().input("Username")
    if not username:
        return
    password = xbmcgui.Dialog().input("Password", option=xbmcgui.ALPHANUM_HIDE_INPUT)
    try:
        result = abs_auth.login(http, address, username, password)
    except abs_http.Unauthorized:
        xbmcgui.Dialog().ok("Kotome", "That username or password was not accepted.")
        return
    except (abs_http.HttpError, abs_http.Unreachable) as error:
        xbmc.log("Kotome: sign-in failed: {}".format(error), xbmc.LOGERROR)
        xbmcgui.Dialog().ok("Kotome", "Sign-in failed: {}".format(error))
        return
    finally:
        http.close()

    if not result.access_token:
        xbmcgui.Dialog().ok("Kotome", "The server did not return a token.")
        return

    creds.apply(result, address=address, server_name=server_name)
    creds.user_name = result.user_name or username
    creds.save()
    invalidate_progress_cache()
    _client_cache["key"] = None
    _notify("Signed in as {}".format(creds.user_name))
    xbmc.log(
        "Kotome: signed in to {} as {}".format(address, creds.user_name), xbmc.LOGINFO
    )


def route_logout():
    creds = abs_auth.Credentials(ADDON)
    if not creds.logged_in:
        _notify("Not signed in")
        return
    if not xbmcgui.Dialog().yesno(
        "Kotome", "Sign out of {}?".format(creds.server_name or creds.server_url)
    ):
        return
    http = abs_auth.transport(verify=_verify_ssl())
    abs_auth.logout(http, creds.server_url, creds.access_token)
    http.close()
    creds.clear()
    invalidate_progress_cache()
    _client_cache["key"] = None
    _notify("Signed out")


def route_test_connection():
    creds = abs_auth.Credentials(ADDON)
    if not creds.has_credentials:
        xbmcgui.Dialog().ok("Kotome", "Sign in first.")
        return
    client = ABSClient.from_credentials(creds, verify=_verify_ssl())
    libraries = client.get_libraries()
    if libraries:
        xbmcgui.Dialog().ok(
            "Kotome",
            "Connected to {}\n\n{} librar{}: {}".format(
                creds.server_name or creds.server_url,
                len(libraries),
                "y" if len(libraries) == 1 else "ies",
                ", ".join(lib.get("name", "?") for lib in libraries),
            ),
        )
        return
    error = client.last_error
    if isinstance(error, abs_http.Unauthorized):
        message = "The server rejected the saved credentials. Sign in again."
    elif error is not None:
        message = "Could not reach the server:\n{}".format(error)
    else:
        message = "Connected, but the account can see no libraries."
    xbmcgui.Dialog().ok("Kotome", message)


def _refresh_after_change():
    invalidate_progress_cache()
    xbmc.executebuiltin("Container.Refresh")


def route_toggle_hide_watched(on):
    ADDON.setSetting("hide_watched", "true" if on else "false")
    _notify("Hiding watched items" if on else "Showing watched items")
    _refresh_after_change()


def route_mark_played(client, item_id, episode_id, played):
    ok = client.set_finished(item_id, episode_id=episode_id, finished=played)
    if ok is None:
        _notify("Could not reach the server")
        return
    _notify("Marked as played" if played else "Marked as unplayed")
    _refresh_after_change()


def route_reset_progress(client, progress_id):
    if client.clear_progress(progress_id) is None:
        _notify("Could not reach the server")
        return
    _notify("Resume position reset")
    _refresh_after_change()


def route_continue_listening(client):
    """Show items currently in progress - books and individual podcast episodes."""
    items = client.get_items_in_progress()
    all_progress = get_progress_map(client)

    for item in items:
        media = item.get("media", {})
        meta = media.get("metadata", {})
        media_type = item.get("mediaType", "book")
        item_id = item["id"]
        authors = _author_entries(client, meta, item.get("libraryId"))
        art = _cover_art(
            client, item, item_id=item_id, artist=_first_artist_art(authors)
        )

        if media_type == "podcast":
            # Show the specific in-progress episode, not the podcast folder
            ep = item.get("recentEpisode")
            if not ep:
                continue
            ep_id = ep.get("id", "")
            ep_title = ep.get("title", "Unknown Episode")
            podcast_title = meta.get("title", "")
            duration = ep.get("audioFile", {}).get("duration", 0)

            # Look up episode progress
            progress_key = "{}-{}".format(item_id, ep_id)
            ep_progress = all_progress.get(progress_key)

            display_title = ep_title

            info = {
                "title": display_title,
                "artist": meta.get("author", ""),
                "cast": authors,
                "album": podcast_title,
                "duration": duration,
                "description": _sanitize_description(ep.get("description", "")),
                "last_played": (ep_progress or {}).get("lastUpdate"),
            }
            play_url = build_url(
                action="play_episode", item_id=item_id, episode_id=ep_id
            )
            add_playable(
                display_title,
                play_url,
                art=art,
                info=info,
                progress=ep_progress,
                context=_abs_context_items(
                    item_id,
                    ep_id,
                    bool((ep_progress or {}).get("isFinished")),
                    ep_progress,
                ),
            )
        else:
            # Book — skip ebook-only items (no audio)
            if media.get("numAudioFiles", 0) == 0 and not media.get("duration"):
                continue
            title = meta.get("title", "Unknown")
            duration = media.get("duration", 0)
            item_progress = all_progress.get(item_id)

            display_title = title

            info = {
                "title": display_title,
                "artist": meta.get("authorName", ""),
                "cast": authors,
                "album": meta.get("seriesName", ""),
                "duration": duration,
                "description": _sanitize_description(meta.get("description", "")),
                "last_played": (item_progress or {}).get("lastUpdate"),
            }
            play_url = build_url(action="play_book", item_id=item_id)
            add_playable(
                display_title,
                play_url,
                art=art,
                info=info,
                progress=item_progress,
                context=_abs_context_items(
                    item_id,
                    finished=bool((item_progress or {}).get("isFinished")),
                    progress=item_progress,
                ),
            )

    # ABS returns these last-played first, which is the order that makes
    # sense here and the one Kodi cannot express as a default.
    _apply_sorts(_SERVER_SORTED)
    xbmcplugin.endOfDirectory(HANDLE)


def route_library(client, library_id, media_type):
    """Show sub-menus for a library."""
    if media_type == "book":
        add_directory(
            "Recently Added",
            action="recently_added",
            library_id=library_id,
        )
        add_directory(
            "All Books",
            action="library_items",
            library_id=library_id,
            media_type=media_type,
        )
        add_directory("Series", action="series_list", library_id=library_id)
        add_directory("Authors", action="authors_list", library_id=library_id)
        add_directory("Collections", action="collections_list", library_id=library_id)
        add_directory(
            "Search", action="search", library_id=library_id, media_type=media_type
        )
    elif media_type == "podcast":
        add_directory(
            "All Podcasts",
            icon=_ACTION_ICONS["podcast_items"],
            action="library_items",
            library_id=library_id,
            media_type=media_type,
        )
        add_directory(
            "Recent Episodes", action="recent_episodes", library_id=library_id
        )
        add_directory(
            "Search", action="search", library_id=library_id, media_type=media_type
        )

    _apply_sorts((xbmcplugin.SORT_METHOD_UNSORTED,), content=CONTENT_MENU)
    xbmcplugin.endOfDirectory(HANDLE)


def _get_page_limit():
    try:
        return int(ADDON.getSetting("items_per_page"))
    except (ValueError, TypeError):
        return 100


def route_library_items(client, library_id, media_type, page=0, sort=None, desc=False):
    """Paginated list of items in a library, sorted server-side."""
    limit = _get_page_limit()
    sort = sort or _DEFAULT_SORT
    # Server-side, not a client-side skip: ABS reports `total` for the filtered
    # set, so the page count and the "Next page" row stay correct. Filtering
    # after the fact would leave short pages and a total that counts rows the
    # user cannot see.
    data = client.get_library_items(
        library_id,
        page=page,
        limit=limit,
        sort=sort,
        desc=desc,
        filter_str=NOT_FINISHED_FILTER if hide_watched() else None,
    )
    if not data:
        xbmcplugin.endOfDirectory(HANDLE)
        return

    results = data.get("results", [])
    total = data.get("total", 0)
    progress_map = get_progress_map(client)

    # The sort picker lives on the context menu, not in the list. As a list
    # item it was a row that sorted into position among the books, appeared
    # only on page one, and had to be scrolled past on every visit. On the
    # context menu it is on every item and every page, which is where a
    # "change how this folder is sorted" control belongs.
    sort_menu = _sort_context_item(library_id, media_type, sort, desc)

    for item in results:
        _add_library_item(
            client, item, media_type, library_id, progress_map, context=[sort_menu]
        )

    # Next page — preserve sort/desc so pagination stays consistent.
    if (page + 1) * limit < total:
        next_args = {
            "action": "library_items",
            "library_id": library_id,
            "media_type": media_type,
            "page": page + 1,
            "sort": sort,
        }
        if desc:
            next_args["desc"] = "1"
        add_directory(
            "[COLOR yellow]Next page ({}/{})[/COLOR]".format(
                page + 2, (total + limit - 1) // limit
            ),
            icon=ICON_NEXT,
            **next_args
        )

    xbmcplugin.setPluginCategory(HANDLE, _sort_label(sort, desc) or "Default order")
    _apply_sorts(_SERVER_SORTED)
    xbmcplugin.endOfDirectory(HANDLE)


def hide_watched():
    return ADDON.getSetting("hide_watched") == "true"


def _hide_watched_context_item():
    on = hide_watched()
    return (
        "Show watched" if on else "Hide watched",
        "RunPlugin({})".format(
            build_url(action="toggle_hide_watched", on="0" if on else "1")
        ),
    )


def _abs_context_items(item_id, episode_id=None, finished=False, progress=None):
    """Mark (un)played and Reset resume, as RunPlugin context entries.

    RunPlugin rather than a directory route: these act and return, and a
    route that returns no listing must not be reachable as one.
    """
    args = {"item_id": item_id}
    if episode_id:
        args["episode_id"] = episode_id
    label = "Mark as unplayed" if finished else "Mark as played"
    items = [
        (
            label,
            "RunPlugin({})".format(
                build_url(action="mark_played", played="0" if finished else "1", **args)
            ),
        )
    ]
    items.append(_hide_watched_context_item())
    progress_id = (progress or {}).get("id")
    if not finished and progress_id:
        items.append(
            (
                "Reset resume position",
                "RunPlugin({})".format(
                    build_url(action="reset_progress", progress_id=progress_id)
                ),
            )
        )
    return items


def _sort_context_item(library_id, media_type, sort, desc):
    """('Sort: Recently added', RunPlugin(...)) for a listing's context menu."""
    return (
        "Sort: {}".format(_sort_label(sort, desc) or "default"),
        "RunPlugin({})".format(
            build_url(
                action="sort_library_items",
                library_id=library_id,
                media_type=media_type,
                sort=sort,
                desc="1" if desc else "0",
            )
        ),
    )


def route_sort_library_items(library_id, media_type, current_sort, current_desc):
    """Sort picker for a library listing, reached from the context menu.

    Invoked with RunPlugin, so there is no directory handle to close and no
    listing to replace — Container.Update does the reload.
    """
    options = [o for o in _SORT_OPTIONS if o[3] in (media_type, "both")]
    labels = [o[0] for o in options]
    preselect = 0
    for i, (_, key, d, _) in enumerate(options):
        if key == current_sort and d == current_desc:
            preselect = i
            break
    choice = xbmcgui.Dialog().select("Sort by", labels, preselect=preselect)
    if choice < 0:
        return
    _, sort_key, desc, _ = options[choice]
    url = build_url(
        action="library_items",
        library_id=library_id,
        media_type=media_type,
        sort=sort_key,
        **({"desc": "1"} if desc else {})
    )
    # replace=true so the picker action doesn't clutter the back-stack.
    xbmc.executebuiltin("Container.Update({},replace)".format(url))


def _add_library_item(
    client, item, media_type, library_id, progress_map=None, context=None
):
    """Add a single book or podcast to the directory listing."""
    media = item.get("media", {})
    meta = media.get("metadata", {})
    title = meta.get("title", "Unknown")
    authors = _author_entries(client, meta, library_id)
    art = _cover_art(client, item, artist=_first_artist_art(authors))
    progress = (progress_map or {}).get(item["id"]) if progress_map else None
    # Series, author and collection listings already spend the single filter
    # ABS allows on the series/author itself, so this one is client-side.
    if hide_watched() and (progress or {}).get("isFinished"):
        return

    if media_type == "podcast":
        num_eps = media.get("numEpisodes", 0)
        label = "{}  [COLOR gray]{} episodes[/COLOR]".format(title, num_eps)
        url = build_url(
            action="podcast_episodes", item_id=item["id"], library_id=library_id
        )
        li = xbmcgui.ListItem(label)
        li.setIsFolder(True)
        li.setArt(art)
        if context:
            li.addContextMenuItems(context)
        tag = li.getVideoInfoTag()
        tag.setMediaType("musicvideo")
        tag.setTitle(title)
        author = meta.get("author", "")
        if author:
            tag.setArtists([author])
        if authors:
            tag.setCast(
                [
                    xbmc.Actor(name, "Author", i, thumb or "")
                    for i, (name, thumb) in enumerate(authors)
                    if name
                ]
            )
        description = meta.get("description", "")
        if description:
            tag.setPlot(_sanitize_description(description))
        genres = meta.get("genres")
        if genres:
            tag.setGenres(genres)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
    else:
        # Book — skip ebook-only items (no audio)
        if media.get("numAudioFiles", 0) == 0 and not media.get("duration"):
            return
        duration = media.get("duration", 0)
        info = {
            "title": title,
            "artist": meta.get("authorName", ""),
            "cast": authors,
            "album": meta.get("seriesName", ""),
            "narrator": meta.get("narratorName", ""),
            "duration": duration,
            "genres": meta.get("genres"),
            "year": meta.get("publishedYear"),
            "description": _sanitize_description(meta.get("description", "")),
            "last_played": (progress or {}).get("lastUpdate"),
        }
        play_url = build_url(action="play_book", item_id=item["id"])
        add_playable(
            title,
            play_url,
            art=art,
            info=info,
            progress=progress,
            context=(context or [])
            + _abs_context_items(
                item["id"],
                finished=bool((progress or {}).get("isFinished")),
                progress=progress,
            ),
        )


def route_series_list(client, library_id, page=0):
    """List all series in a book library."""
    limit = _get_page_limit()
    data = client.get_series(library_id, page=page, limit=limit)
    if not data:
        xbmcplugin.endOfDirectory(HANDLE)
        return

    results = data.get("results", [])
    total = data.get("total", 0)

    for series in results:
        name = series.get("name", "Unknown")
        books = series.get("books", [])
        label = "{}  [COLOR gray]{} books[/COLOR]".format(name, len(books))
        add_directory(
            label, action="series_detail", library_id=library_id, series_id=series["id"]
        )

    if (page + 1) * limit < total:
        add_directory(
            "[COLOR yellow]Next page[/COLOR]",
            icon=ICON_NEXT,
            action="series_list",
            library_id=library_id,
            page=page + 1,
        )

    _apply_sorts(_NAME_SORTS, content=CONTENT_MENU)
    xbmcplugin.endOfDirectory(HANDLE)


def route_series_detail(client, library_id, series_id):
    """Show books in a series (filter library items by series ID)."""
    filter_str = "series." + b64encode(series_id.encode()).decode()
    data = client.get_library_items(library_id, limit=100, filter_str=filter_str)
    if data:
        progress_map = get_progress_map(client)
        for item in data.get("results", []):
            _add_library_item(client, item, "book", library_id, progress_map)
    _apply_sorts(_SERVER_SORTED)
    xbmcplugin.endOfDirectory(HANDLE)


def route_authors_list(client, library_id):
    """List all authors."""
    authors = client.get_authors(library_id)
    for author in sorted(authors, key=lambda a: a.get("name", "")):
        name = author.get("name", "Unknown")
        count = author.get("numBooks", 0)
        label = "{}  [COLOR gray]{} books[/COLOR]".format(name, count)
        if author.get("imagePath"):
            image = client.author_image_url(author["id"])
        else:
            image = _ACTION_ICONS["authors_list"]
        art = {"thumb": image, "poster": image, "icon": image, "artist": image}
        url = build_url(
            action="author_books",
            library_id=library_id,
            author_id=author["id"],
            author_name=name,
        )
        li = xbmcgui.ListItem(label)
        li.setIsFolder(True)
        li.setArt(art)
        tag = li.getVideoInfoTag()
        tag.setMediaType("musicvideo")
        tag.setTitle(name)
        tag.setArtists([name])
        tag.setCast([xbmc.Actor(name, "Author", 0, image)])
        description = author.get("description", "")
        if description:
            tag.setPlot(_sanitize_description(description))
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)

    _apply_sorts(_NAME_SORTS, content=CONTENT_MENU)
    xbmcplugin.endOfDirectory(HANDLE)


def route_author_books(client, library_id, author_id, author_name):
    """Show books by a specific author (filter library items by author ID)."""
    filter_str = "authors." + b64encode(author_id.encode()).decode()
    data = client.get_library_items(library_id, limit=100, filter_str=filter_str)
    if data:
        progress_map = get_progress_map(client)
        for item in data.get("results", []):
            _add_library_item(client, item, "book", library_id, progress_map)
    _apply_sorts(_SERVER_SORTED)
    xbmcplugin.endOfDirectory(HANDLE)


def route_collections_list(client, library_id):
    """List all collections."""
    collections = client.get_collections(library_id)
    for col in collections:
        name = col.get("name", "Unknown")
        books = col.get("books", [])
        label = "{}  [COLOR gray]{} books[/COLOR]".format(name, len(books))
        add_directory(
            label,
            action="collection_detail",
            library_id=library_id,
            collection_id=col["id"],
        )
    _apply_sorts(_NAME_SORTS, content=CONTENT_MENU)
    xbmcplugin.endOfDirectory(HANDLE)


def route_collection_detail(client, library_id, collection_id):
    """Show books in a collection."""
    data = client._get("/api/collections/{}".format(collection_id))
    if data:
        progress_map = get_progress_map(client)
        for item in data.get("books", []):
            _add_library_item(client, item, "book", library_id, progress_map)
    _apply_sorts(_SERVER_SORTED)
    xbmcplugin.endOfDirectory(HANDLE)


def route_podcast_episodes(client, item_id, library_id):
    """List episodes for a podcast."""
    data = client.get_item(item_id, expanded=True)
    if not data:
        xbmcplugin.endOfDirectory(HANDLE)
        return

    media = data.get("media", {})
    meta = media.get("metadata", {})
    podcast_title = meta.get("title", "")
    episodes = media.get("episodes", [])
    progress_map = get_progress_map(client)
    # One lookup: every episode of a podcast shows the show's cover.
    podcast_art = _cover_art(client, data, item_id=item_id)

    # Sort by most recent first
    episodes.sort(key=lambda e: e.get("publishedAt", 0) or 0, reverse=True)

    for ep in episodes:
        ep_id = ep.get("id", "")
        ep_title = ep.get("title", "Unknown Episode")
        duration = ep.get("audioFile", {}).get("duration", 0)
        ep_progress = progress_map.get("{}-{}".format(item_id, ep_id))
        if hide_watched() and (ep_progress or {}).get("isFinished"):
            continue

        art = podcast_art
        info = {
            "title": ep_title,
            "album": podcast_title,
            "duration": duration,
            "description": _sanitize_description(ep.get("description", "")),
            "last_played": (ep_progress or {}).get("lastUpdate"),
        }
        play_url = build_url(action="play_episode", item_id=item_id, episode_id=ep_id)
        add_playable(
            ep_title,
            play_url,
            art=art,
            info=info,
            progress=ep_progress,
            context=_abs_context_items(
                item_id, ep_id, bool((ep_progress or {}).get("isFinished")), ep_progress
            ),
        )

    # Already sorted newest-first above; UNSORTED keeps that as the default.
    _apply_sorts(_EPISODE_SORTS)
    xbmcplugin.endOfDirectory(HANDLE)


def route_recent_episodes(client, library_id):
    """Show recently added podcast episodes.

    Same shape as Continue Listening: a playable row per episode, the
    episode title as the label, the show in the album tag. ABS nests the
    podcast (with coverPath) on each episode; the audio-file album tag is
    not the show name and is not a cover.
    """
    data = client.get_recent_episodes(library_id, limit=50)
    if not data:
        xbmcplugin.endOfDirectory(HANDLE)
        return

    progress_map = get_progress_map(client)
    episodes = data.get("episodes", [])
    for ep in episodes:
        item_id = ep.get("libraryItemId", "")
        ep_id = ep.get("id", "")
        ep_title = ep.get("title", "Unknown")
        podcast = ep.get("podcast") or {}
        podcast_meta = podcast.get("metadata") or {}
        podcast_title = (
            podcast_meta.get("title")
            or podcast.get("title")
            or ((ep.get("audioFile") or {}).get("metaTags") or {}).get("tagAlbum")
            or ""
        )
        duration = (
            (ep.get("audioFile") or {}).get("duration") or ep.get("duration") or 0
        )
        ep_progress = progress_map.get("{}-{}".format(item_id, ep_id))
        if hide_watched() and (ep_progress or {}).get("isFinished"):
            continue

        cover_source = ep.get("libraryItem") or {
            "media": podcast,
            "coverPath": podcast.get("coverPath"),
        }
        art = _cover_art(client, cover_source, item_id=item_id)
        authors = _author_entries(client, podcast_meta)
        info = {
            "title": ep_title,
            "artist": podcast_meta.get("author", ""),
            "cast": authors,
            "album": podcast_title,
            "duration": duration,
            "description": _sanitize_description(ep.get("description", "")),
            "last_played": (ep_progress or {}).get("lastUpdate"),
        }
        play_url = build_url(action="play_episode", item_id=item_id, episode_id=ep_id)
        add_playable(
            ep_title,
            play_url,
            art=art,
            info=info,
            progress=ep_progress,
            context=_abs_context_items(
                item_id, ep_id, bool((ep_progress or {}).get("isFinished")), ep_progress
            ),
        )

    # Newest-first from the server; registering title sort would reorder it.
    _apply_sorts(_SERVER_SORTED)
    xbmcplugin.endOfDirectory(HANDLE)


def route_recently_added(client, library_id):
    """Recently added books in one library, newest first."""
    if not library_id:
        xbmcplugin.endOfDirectory(HANDLE)
        return
    data = client.get_library_items(
        library_id,
        page=0,
        limit=50,
        sort="addedAt",
        desc=True,
        filter_str=NOT_FINISHED_FILTER if hide_watched() else None,
    )
    if not data:
        xbmcplugin.endOfDirectory(HANDLE)
        return
    progress_map = get_progress_map(client)
    for item in data.get("results", []):
        _add_library_item(client, item, "book", library_id, progress_map)

    _apply_sorts(_SERVER_SORTED)
    xbmcplugin.endOfDirectory(HANDLE)


def route_search(client, library_id, media_type):
    """Prompt user for search query and show results."""
    kb = xbmc.Keyboard("", "Search")
    kb.doModal()
    if not kb.isConfirmed():
        xbmcplugin.endOfDirectory(HANDLE)
        return

    query = kb.getText().strip()
    if not query:
        xbmcplugin.endOfDirectory(HANDLE)
        return

    data = client.search(library_id, query)
    if not data:
        xbmcplugin.endOfDirectory(HANDLE)
        return

    progress_map = get_progress_map(client)
    if media_type == "book":
        for entry in data.get("book", []):
            item = entry.get("libraryItem", entry)
            _add_library_item(client, item, "book", library_id, progress_map)
        for entry in data.get("series", []):
            series = entry.get("series", entry)
            name = series.get("name", "Unknown")
            books = series.get("books", [])
            label = "[Series] {}  [COLOR gray]{} books[/COLOR]".format(name, len(books))
            add_directory(
                label,
                action="series_detail",
                library_id=library_id,
                series_id=series["id"],
            )
        for entry in data.get("authors", []):
            author = entry.get("author", entry)
            name = author.get("name", "Unknown")
            label = "[Author] {}".format(name)
            add_directory(
                label,
                action="author_books",
                library_id=library_id,
                author_id=author["id"],
                author_name=name,
            )
    elif media_type == "podcast":
        for entry in data.get("podcast", []):
            item = entry.get("libraryItem", entry)
            _add_library_item(client, item, "podcast", library_id, progress_map)

    _apply_sorts(_SERVER_SORTED if media_type == "book" else _NAME_SORTS)
    xbmcplugin.endOfDirectory(HANDLE)


# ── Playback ──

SESSION_FILE = os.path.join(PROFILE_DIR, "session.json")
SPEEDS_FILE = os.path.join(PROFILE_DIR, "speeds.json")
SLEEP_FILE = os.path.join(PROFILE_DIR, "sleep_timer")
# Our own rate and config files, named in the sentinel so inputstream.tempo's
# keymap acts on ours and not on another add-on's. A patched YouTube drives
# the same add-on; on the shared paths an audiobook at 2.0x and a video at
# 1.5x overwrote each other's rate.
OWNER = "plugin.audio.kotome"
TEMPO_FILE = xbmcvfs.translatePath("special://temp/inputstream_tempo." + OWNER)
CONFIG_FILE = xbmcvfs.translatePath("special://temp/inputstream_tempo_config." + OWNER)
# The sentinel stays shared: it is the single "the keys are live" flag.
ACTIVE_FILE = xbmcvfs.translatePath("special://temp/inputstream_tempo_active")


def _save_session(data):
    """Write session info to disk for the background service to pick up."""
    os.makedirs(PROFILE_DIR, exist_ok=True)
    with open(SESSION_FILE, "w") as f:
        json.dump(data, f)


def _get_float(setting_id, default):
    try:
        return float(ADDON.getSetting(setting_id))
    except (ValueError, TypeError):
        return default


def _speed_config():
    """Return (step, min, max) from settings, with sane defaults."""
    step = _get_float("speed_step", 0.10)
    lo = _get_float("min_speed", 1.0)
    hi = _get_float("max_speed", 3.0)
    # Defensive: make sure min <= max; fall back to sane range if inverted.
    if lo > hi:
        lo, hi = 0.5, 5.0
    return step, lo, hi


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _get_tempo(media_type="book"):
    """Default playback speed for the given media type, clamped to min/max."""
    _step, lo, hi = _speed_config()
    raw = _get_float("podcast_speed" if media_type == "podcast" else "book_speed", 1.0)
    return round(_clamp(raw, lo, hi), 2)


def _write_tempo(tempo):
    """Write tempo value to our inputstream.tempo rate file.

    Atomically — the add-on polls it every 250 ms and would otherwise be
    able to read a half-written value.
    """
    tmp = TEMPO_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(tempo))
    os.replace(tmp, TEMPO_FILE)


def _write_config_file():
    """Write {step, min, max} as JSON for inputstream.tempo's speed.py."""
    step, lo, hi = _speed_config()
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump({"step": step, "min": lo, "max": hi}, f)
    except IOError:
        pass


def _load_book_speed(item_id):
    """Load saved speed for a specific book. Returns None if not found."""
    try:
        if os.path.exists(SPEEDS_FILE):
            with open(SPEEDS_FILE, "r") as f:
                speeds = json.load(f)
                return speeds.get(item_id)
    except Exception:
        pass
    return None


def _save_book_speed(item_id, speed):
    """Save speed for a specific book."""
    speeds = {}
    try:
        if os.path.exists(SPEEDS_FILE):
            with open(SPEEDS_FILE, "r") as f:
                speeds = json.load(f)
    except Exception:
        pass
    speeds[item_id] = speed
    os.makedirs(PROFILE_DIR, exist_ok=True)
    with open(SPEEDS_FILE, "w") as f:
        json.dump(speeds, f)


def _resolve_playback(client, item_id, episode_id=None):
    """Create an ABS session and resolve the stream URL via inputstream.tempo."""
    # Whatever is about to play will have moved by the time the user is back
    # in a listing, so don't let the cached map outlive this.
    invalidate_progress_cache()
    # Both queues, still: a profile that used the old Music player setting can
    # have items parked in the music playlist from before the switch.
    xbmc.PlayList(xbmc.PLAYLIST_MUSIC).clear()
    xbmc.PlayList(xbmc.PLAYLIST_VIDEO).clear()

    session = client.start_playback(item_id, episode_id=episode_id)
    if not session:
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return

    tracks = session.get("audioTracks", [])
    if not tracks:
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return

    meta = session.get("mediaMetadata", {})
    # displayTitle includes the episode name for podcasts; fall back to item title
    title = session.get("displayTitle") or meta.get("title", "")
    authors = meta.get("authors", [])
    author_str = (
        ", ".join(a.get("name", "") for a in authors)
        if authors
        else meta.get("author", "")
    )
    cover_url = client.cover_url(item_id)
    start_time = session.get("currentTime", 0)
    duration = session.get("duration", 0)
    description = _sanitize_description(meta.get("description", ""))

    # Save session info for the background service (handles sync + resume seek)
    _save_session(
        {
            "session_id": session["id"],
            "item_id": item_id,
            "episode_id": episode_id,
            "duration": duration,
            "start_time": start_time,
            "started_at": time.time(),
            "chapters": session.get("chapters", []),
            "media_metadata": {
                "title": title,
                "author": author_str,
            },
        }
    )

    track = tracks[0]
    url = client.stream_url(track["contentUrl"])

    # Per-item speed takes priority over global setting
    # For podcasts, speed is keyed by podcast item_id (shared across episodes)
    media_type = session.get("mediaType", "book")
    use_per_item = ADDON.getSetting("per_book_speed") != "false"
    saved_speed = _load_book_speed(item_id) if use_per_item else None
    raw_tempo = saved_speed if saved_speed is not None else _get_tempo(media_type)
    # Clamp against current settings in case min/max has been tightened since save.
    _step, lo, hi = _speed_config()
    tempo = round(_clamp(raw_tempo, lo, hi), 2)
    _write_tempo(tempo)
    _write_config_file()
    # Sentinel — tells inputstream.tempo keys/dialog they can act, and which
    # files to act on. Service clears this on playback stop, so non-tempo
    # playback gets a no-op.
    try:
        with open(ACTIVE_FILE, "w") as f:
            f.write(
                "addon={}\ntempo_file={}\nconfig_file={}\n".format(
                    OWNER, TEMPO_FILE, CONFIG_FILE
                )
            )
    except IOError:
        pass

    authors_meta = _author_entries(client, meta)
    artist_thumb = _first_artist_art(authors_meta)
    art = {"thumb": cover_url, "poster": cover_url, "fanart": cover_url}
    if artist_thumb:
        art["artist"] = artist_thumb

    li = xbmcgui.ListItem(path=url)
    li.setArt(art)
    li.setContentLookup(False)

    podcast_name = meta.get("title", "")

    # mediaType=musicvideo routes the ListItem to VideoPlayer while still
    # landing in WINDOW_VISUALISATION for audio-only content. VideoInfoTag
    # fields populate the now-playing OSD and the Info dialog.
    vtag = li.getVideoInfoTag()
    vtag.setTitle(title)
    if author_str:
        vtag.setArtists([author_str])
    if authors_meta:
        vtag.setCast(
            [
                xbmc.Actor(name, "Author", i, thumb or "")
                for i, (name, thumb) in enumerate(authors_meta)
                if name
            ]
        )
    if episode_id and podcast_name:
        vtag.setAlbum(podcast_name)
    if description:
        vtag.setPlot(description)
    vtag.setDuration(int(duration))
    vtag.setMediaType("musicvideo")

    # Route through inputstream.tempo for playback speed control
    li.setProperty("inputstream", "inputstream.tempo")
    li.setProperty("inputstream.tempo.mime_type", track.get("mimeType", "audio/mp4"))
    if tempo != 1.0:
        li.setProperty("inputstream.tempo.tempo", str(tempo))
    li.setProperty("inputstream.tempo.tempo_file", TEMPO_FILE)

    if start_time > 0:
        # inputstream.tempo.start_time arms a player-agnostic hold inside
        # the addon that gates packet output until a real seek arrives, so
        # no pts=0 audio reaches the sink before the resume seek lands.
        li.setProperty("inputstream.tempo.start_time", str(start_time))
        # VideoPlayer reads StartOffset (ms) and issues a SeekTime after
        # demuxer open. ResumeTime/TotalTime keep the resume dialog and OSD
        # progress consistent.
        li.setProperty("StartOffset", str(int(start_time * 1000)))
        li.setProperty("ResumeTime", str(int(start_time)))
        li.setProperty("TotalTime", str(int(duration)))

    xbmcplugin.setResolvedUrl(HANDLE, True, li)


def route_play_book(client, item_id):
    _resolve_playback(client, item_id)


def route_play_episode(client, item_id, episode_id):
    _resolve_playback(client, item_id, episode_id=episode_id)


# ── Router ──


def router():
    """Parse the plugin URL and dispatch to the right handler."""
    _refresh_invocation()
    # Kodi writes library-node and favourite paths with a trailing slash, and
    # it lands on whichever query parameter comes last — not necessarily
    # 'action'. Stripping it off the action alone fixed only the routes that
    # take no other parameter: '&media_type=book/' matched neither branch of
    # route_library and returned an empty folder with no error at all.
    params = parse_qs(sys.argv[2][1:].rstrip("/"))

    # Unwrap single-value lists
    args = {}
    for k, v in params.items():
        args[k] = v[0] if len(v) == 1 else v

    action = args.get("action", "")

    # Account routes run before there is a client, and are reached with
    # RunPlugin from the settings buttons, so there is no handle either.
    if action == "login":
        return route_login()
    if action == "logout":
        return route_logout()
    if action == "test_connection":
        return route_test_connection()
    if action == "find_servers":
        return route_find_servers()
    if action == "settings":
        return route_settings()

    client = get_client()
    if not client:
        if not action:
            route_root_signed_out()
        else:
            xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return

    if not action:
        route_root(client)
    elif action == "continue_listening":
        route_continue_listening(client)
    elif action == "recently_added":
        route_recently_added(client, args.get("library_id") or None)
    elif action == "library":
        route_library(client, args["library_id"], args["media_type"])
    elif action == "library_items":
        route_library_items(
            client,
            args["library_id"],
            args["media_type"],
            page=int(args.get("page", 0)),
            sort=args.get("sort"),
            desc=args.get("desc") == "1",
        )
    elif action == "sort_library_items":
        route_sort_library_items(
            args["library_id"],
            args["media_type"],
            current_sort=args.get("sort"),
            current_desc=args.get("desc") == "1",
        )
    elif action == "series_list":
        route_series_list(client, args["library_id"], page=int(args.get("page", 0)))
    elif action == "series_detail":
        route_series_detail(client, args["library_id"], args["series_id"])
    elif action == "authors_list":
        route_authors_list(client, args["library_id"])
    elif action == "author_books":
        route_author_books(
            client, args["library_id"], args["author_id"], args["author_name"]
        )
    elif action == "collections_list":
        route_collections_list(client, args["library_id"])
    elif action == "collection_detail":
        route_collection_detail(client, args["library_id"], args["collection_id"])
    elif action == "podcast_episodes":
        route_podcast_episodes(client, args["item_id"], args.get("library_id", ""))
    elif action == "recent_episodes":
        route_recent_episodes(client, args["library_id"])
    elif action == "search":
        route_search(client, args["library_id"], args["media_type"])
    elif action == "play_book":
        route_play_book(client, args["item_id"])
    elif action == "play_episode":
        route_play_episode(client, args["item_id"], args["episode_id"])
    elif action == "toggle_hide_watched":
        route_toggle_hide_watched(args.get("on") == "1")
    elif action == "mark_played":
        route_mark_played(
            client,
            args["item_id"],
            args.get("episode_id"),
            args.get("played") == "1",
        )
    elif action == "reset_progress":
        route_reset_progress(client, args["progress_id"])
    elif action == "speed_dialog":
        route_speed_dialog()
    elif action == "set_sleep_timer":
        route_set_sleep_timer()
    else:
        # An unrecognised route still has to close its handle. Without this the
        # caller waits in CScriptRunner's first loop, which has no timeout at
        # all — harmless today only because the interpreter dies and releases
        # it, and an unbounded hang the moment reuselanguageinvoker is on.
        xbmc.log("Kotome: unknown action {!r}".format(action), xbmc.LOGWARNING)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


if __name__ == "__main__":
    router()
