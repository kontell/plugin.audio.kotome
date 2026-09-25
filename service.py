"""Kotome - background service for playback progress sync, resume, and audiobook features."""

import json
import os
import time

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

import abs_auth
import addonxml
from abs_api import ABSClient

ADDON = xbmcaddon.Addon()
PROFILE_DIR = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
SESSION_FILE = os.path.join(PROFILE_DIR, "session.json")
SPEEDS_FILE = os.path.join(PROFILE_DIR, "speeds.json")
SLEEP_FILE = os.path.join(PROFILE_DIR, "sleep_timer")
SLEEP_STATE_FILE = os.path.join(PROFILE_DIR, "sleep_state.json")
# Our own rate and config files, named in the sentinel so inputstream.tempo's
# keymap acts on ours and not on another add-on's. A patched YouTube drives
# the same add-on; on the shared paths an audiobook at 2.0x and a video at
# 1.5x overwrote each other's rate.
OWNER = "plugin.audio.kotome"
TEMPO_FILE = xbmcvfs.translatePath("special://temp/inputstream_tempo." + OWNER)
CONFIG_FILE = xbmcvfs.translatePath("special://temp/inputstream_tempo_config." + OWNER)
# The sentinel stays shared: it is the single "the keys are live" flag.
ACTIVE_FILE = xbmcvfs.translatePath("special://temp/inputstream_tempo_active")


def _sentinel_owner():
    """The add-on named in the sentinel, or None if it is not in keyed form."""
    try:
        with open(ACTIVE_FILE) as f:
            content = f.read()
    except (IOError, OSError):
        return None
    for line in content.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "addon":
            return value.strip()
    return None


def _release_sentinel():
    """Give the tempo speed keys back, if we were the ones holding them.

    Kodi's player state is global, so this stop path also runs when something
    else was playing — removing the sentinel unconditionally took the keys
    away from whichever add-on had armed them.
    """
    if _sentinel_owner() != OWNER:
        return
    try:
        os.remove(ACTIVE_FILE)
    except OSError:
        pass


def _get_float(setting_id, default):
    try:
        return float(ADDON.getSetting(setting_id))
    except (ValueError, TypeError):
        return default


def _addon_xml_path():
    return os.path.join(xbmcvfs.translatePath(ADDON.getAddonInfo("path")), "addon.xml")


def _reuse_invoker_wanted():
    return ADDON.getSetting("reuse_language_invoker") != "false"


def apply_reuse_invoker(notify=False):
    """Align addon.xml with the setting. True if the file changed."""
    wrote = addonxml.apply(_reuse_invoker_wanted(), _addon_xml_path())
    if wrote is None:
        xbmc.log(
            "Kotome: reuse language invoker change did not land on addon.xml",
            xbmc.LOGWARNING,
        )
        return False
    if wrote and notify:
        xbmcgui.Dialog().notification(
            "Kotome",
            ADDON.getLocalizedString(30192) or "Restart Kodi to apply this",
            time=4000,
        )
    return bool(wrote)


def write_config():
    """Write {step, min, max} as JSON for speed.py to consume."""
    step = _get_float("speed_step", 0.10)
    lo = _get_float("min_speed", 1.0)
    hi = _get_float("max_speed", 3.0)
    if lo > hi:
        lo, hi = 0.5, 5.0
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump({"step": step, "min": lo, "max": hi}, f)
    except IOError:
        pass


def load_session():
    try:
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def clear_session():
    try:
        if os.path.exists(SESSION_FILE):
            os.remove(SESSION_FILE)
    except Exception:
        pass


def read_tempo():
    try:
        with open(TEMPO_FILE) as f:
            return float(f.read().strip())
    except Exception:
        return 1.0


def save_book_speed(item_id, speed):
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


def get_client():
    """The API client for the signed-in user, or None.

    Reads the same stored credentials main.py writes, and gets the same
    refresh-on-401 behaviour: the service outlives any one access token by
    hours, so this is the process that needs it most.
    """
    creds = abs_auth.Credentials(ADDON)
    if not creds.has_credentials:
        return None
    return ABSClient.from_credentials(
        creds, verify=ADDON.getSetting("ssl_verify") != "false"
    )


def find_chapter(chapters, current_time):
    """Find the current chapter name given playback position in seconds."""
    for ch in chapters:
        if ch.get("start", 0) <= current_time < ch.get("end", 0):
            return ch.get("title", "")
    return ""


def _close_active_session(client, session, label="session"):
    """Sync + close an ABS session, but never sync position=0 over a valid
    resume point — playback may have failed to start (e.g. HTTP error),
    leaving last_time at 0 even though start_time was the user's bookmark.
    Syncing 0 back to ABS would clobber the bookmark and remove the book
    from Continue Listening.
    """
    if not (client and session):
        return
    sid = session.get("session_id")
    last = session.get("last_time", 0) or 0
    start = session.get("start_time", 0) or 0
    dur = session.get("duration", 0)
    try:
        if last < 5 and start > 30:
            xbmc.log(
                "Kotome: skip sync on close ({} last={:.0f}s, "
                "start={:.0f}s — playback never resumed)".format(label, last, start),
                xbmc.LOGINFO,
            )
        else:
            client.sync_session(sid, last, dur, 0)
        client.close_session(sid)
        xbmc.log("Kotome: closed {} {}".format(label, sid), xbmc.LOGINFO)
    except Exception as e:
        xbmc.log("Kotome: error closing {}: {}".format(label, e), xbmc.LOGWARNING)


def _jsonrpc(method, params=None):
    """Run a JSON-RPC call and return the result dict (or {})."""
    try:
        req = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            req["params"] = params
        resp = json.loads(xbmc.executeJSONRPC(json.dumps(req)))
        return resp.get("result", {}) or {}
    except Exception as e:
        xbmc.log("Kotome: JSON-RPC error ({}): {}".format(method, e), xbmc.LOGWARNING)
        return {}


# Consecutive polls of an inactive screensaver that mean the user is really
# back, rather than the momentary drop every Player.Stop causes. At the loop's
# 0.25 s tick this is a fifth of a second of confirmation.
WAKE_CONFIRM_POLLS = 3

# Kodi settings the screensaver action has to borrow, and put back.
SETTING_SAVER_MODE = "screensaver.mode"
SETTING_SAVER_AUDIO = "screensaver.disableforaudio"

# Screensaver add-ons Kodi always ships, used when an action names a look.
SAVER_BLACK = "screensaver.xbmc.builtin.black"
SAVER_DIM = "screensaver.xbmc.builtin.dim"

_SAVER_FOR_ACTION = {
    "screensaver_black": SAVER_BLACK,
    "screensaver_dim": SAVER_DIM,
    "screensaver_user": None,  # whatever the user already configured
}

# Values written by versions that drove the screen directly. Both mechanisms
# they named are no-ops on Android and neither survived testing on Linux, so
# they are folded into the screensaver path rather than left to fail quietly.
_LEGACY_ACTIONS = {
    "screen_off_cec": "screensaver_black",
    "screen_off_android": "screen_off",
}


def _normalise_screen_action(value):
    """Current name for a possibly-legacy sleep_screen_action value."""
    return _LEGACY_ACTIONS.get(value, value or "screensaver_black")


def _get_setting(setting_id):
    """Read a Kodi (not add-on) setting, or None if it could not be read."""
    result = _jsonrpc("Settings.GetSettingValue", {"setting": setting_id})
    return result.get("value") if "value" in result else None


def _set_setting(setting_id, value):
    _jsonrpc("Settings.SetSettingValue", {"setting": setting_id, "value": value})


# Window properties are a public interface: a skin can be keyed on them, and
# renaming one breaks it with no warning and no error. Both names are written
# for the whole 1.x line; the Koshelf.* aliases go in 2.0.
_PROPERTY_PREFIXES = ("Kotome", "Koshelf")

_PROPERTY_NAMES = (
    "ChapterName",
    "NowPlaying.Title",
    "NowPlaying.Author",
    "SleepTimerRemaining",
)


def _set_property(win, name, value):
    for prefix in _PROPERTY_PREFIXES:
        win.setProperty("{}.{}".format(prefix, name), value)


def _clear_property(win, name):
    for prefix in _PROPERTY_PREFIXES:
        win.clearProperty("{}.{}".format(prefix, name))


class SleepModeController:
    """Owns sleep-timer side effects: screen action, volume ramp-down, and
    crash recovery.

    The controller is driven by the existence of SLEEP_FILE — main.py writes
    it (epoch seconds = end_time) to start a timer. The service polls it and
    when the file appears we save state and begin watching for idle/ramp-down.
    When the file disappears (expiry or external cancel) we restore everything.

    Screen action modes (sleep_screen_action setting):
      none              — don't touch the screen
      screensaver_black — Kodi's screensaver, forced to Black, after idle
      screensaver_dim   — Kodi's screensaver, forced to Dim, after idle
      screensaver_user  — Kodi's screensaver, the user's own choice
      screen_off        — power the display down via DPMS (Linux/X11 only)

    Earlier versions drew a full-screen xbmcgui.WindowDialog instead of using
    Kodi's screensaver. That is gone. A Python window outlives the script that
    created it, survives the add-on being disabled, and once ownerless the next
    Back press deadlocks Kodi's application thread inside
    CScriptInvocationManager::Process() — an unrecoverable whole-UI freeze that
    lands on the user while they are asleep. Verified on 21.3 with a gdb
    backtrace; only SIGKILL ended it.

    Two Kodi settings have to be borrowed for the screensaver path and put back
    afterwards: screensaver.mode, because an empty mode ("None") draws nothing,
    and screensaver.disableforaudio, which defaults to true and otherwise makes
    ActivateScreenSaver a silent no-op during audio playback.
    """

    def __init__(self):
        self.active = False
        self.original_volume = None
        self.last_applied_volume = None
        self.user_overrode_volume = False
        self._screen_action_fired = False
        self._screen_action = "none"
        self._saved_saver_mode = None
        self._saved_saver_audio = None
        self._awaiting_wake = False
        self._redarken_pending = False
        self._wake_polls = 0
        if os.path.exists(SLEEP_STATE_FILE) and not os.path.exists(SLEEP_FILE):
            self._restore_from_state_file()

    def _restore_from_state_file(self):
        """Put back what a Kodi killed mid-timer left changed.

        Without this the user is left with their screensaver disabled, audio
        screensavers silently re-enabled, and the volume wherever the ramp had
        got to — permanently, with nothing to explain it.
        """
        try:
            with open(SLEEP_STATE_FILE) as f:
                state = json.load(f)
            vol = state.get("volume")
            if vol is not None:
                _jsonrpc("Application.SetVolume", {"volume": int(vol)})
            if state.get("saver_mode") is not None:
                _set_setting(SETTING_SAVER_MODE, state["saver_mode"])
            if state.get("saver_audio") is not None:
                _set_setting(SETTING_SAVER_AUDIO, bool(state["saver_audio"]))
            if state.get("screen_action") == "screen_off":
                if xbmc.getCondVisibility("System.DPMSActive"):
                    xbmc.executebuiltin("ToggleDPMS")
            os.remove(SLEEP_STATE_FILE)
            xbmc.log("Kotome: restored orphaned sleep-mode state", xbmc.LOGINFO)
        except Exception as e:
            xbmc.log(
                "Kotome: error restoring sleep state: {}".format(e), xbmc.LOGWARNING
            )

    def tick(self, player, win):
        """Poll once. Called from the service loop while playback is active.

        Returns True if the timer expired this tick (caller may want to
        skip remaining work), False otherwise.
        """
        sleep_file_exists = os.path.exists(SLEEP_FILE)

        if sleep_file_exists and not self.active:
            self._enter()
        elif not sleep_file_exists and self.active:
            self._exit()
            _clear_property(win, "SleepTimerRemaining")
            return False

        if not self.active:
            return False

        try:
            with open(SLEEP_FILE) as f:
                end_time = float(f.read().strip())
        except Exception:
            self._exit()
            _clear_property(win, "SleepTimerRemaining")
            return False

        remaining = end_time - time.time()

        if remaining <= 0:
            self._expire(player)
            _clear_property(win, "SleepTimerRemaining")
            return True

        mins = int(remaining) // 60
        secs = int(remaining) % 60
        _set_property(win, "SleepTimerRemaining", "{}:{:02d}".format(mins, secs))

        self._maybe_fire_screen_action()
        self._maybe_ramp_volume(remaining)
        return False

    def on_playback_stopped(self, win):
        """Called when playback stops while we're not in tick(). Cancels the
        timer and restores state so a manual stop doesn't leave the screen
        dimmed or volume faded."""
        if os.path.exists(SLEEP_FILE):
            try:
                os.remove(SLEEP_FILE)
            except OSError:
                pass
        if self.active:
            self._exit()
        _clear_property(win, "SleepTimerRemaining")

    def _enter(self):
        self.active = True
        self.user_overrode_volume = False
        self._screen_action_fired = False
        self._screen_action = _normalise_screen_action(
            ADDON.getSetting("sleep_screen_action")
        )
        if self._screen_action == "screen_off" and xbmc.getCondVisibility(
            "System.Platform.Android"
        ):
            # Kodi has no DPMS implementation on Android: the builtin returns
            # without acting and without logging. Fall back to the screensaver
            # rather than promise a dark screen we cannot deliver.
            xbmc.log(
                "Kotome: screen_off is Linux/X11 only — using the screensaver "
                "on this platform",
                xbmc.LOGINFO,
            )
            self._screen_action = "screensaver_black"

        vol = _jsonrpc("Application.GetProperties", {"properties": ["volume"]}).get(
            "volume"
        )
        self.original_volume = vol
        self.last_applied_volume = vol

        self._saved_saver_mode = None
        self._saved_saver_audio = None
        if self._screen_action in _SAVER_FOR_ACTION:
            self._saved_saver_mode = _get_setting(SETTING_SAVER_MODE)
            self._saved_saver_audio = _get_setting(SETTING_SAVER_AUDIO)

        try:
            with open(SLEEP_STATE_FILE, "w") as f:
                json.dump(
                    {
                        "volume": vol,
                        "screen_action": self._screen_action,
                        "saver_mode": self._saved_saver_mode,
                        "saver_audio": self._saved_saver_audio,
                    },
                    f,
                )
        except IOError:
            pass
        xbmc.log(
            "Kotome: sleep mode entered (action={}, "
            "volume={})".format(self._screen_action, vol),
            xbmc.LOGINFO,
        )

    def _exit(self):
        """Restore screen and volume — called on cancel or manual stop.

        The user is present, so this wakes the screen as well as putting the
        settings back.
        """
        if self._screen_action_fired:
            self._wake_screen()
        self._restore_saver_settings()
        if self.original_volume is not None and not self.user_overrode_volume:
            _jsonrpc("Application.SetVolume", {"volume": int(self.original_volume)})
        try:
            if os.path.exists(SLEEP_STATE_FILE):
                os.remove(SLEEP_STATE_FILE)
        except OSError:
            pass
        xbmc.log("Kotome: sleep mode exited", xbmc.LOGINFO)
        self._reset()

    def _expire(self, player):
        """Timer fired — stop playback, restore volume, leave screen dark."""
        xbmc.log("Kotome: sleep timer expired, stopping playback", xbmc.LOGINFO)
        try:
            player.stop()
        except Exception:
            pass
        try:
            os.remove(SLEEP_FILE)
        except OSError:
            pass
        if self.original_volume is not None and not self.user_overrode_volume:
            _jsonrpc("Application.SetVolume", {"volume": int(self.original_volume)})

        if self._screen_action_fired and self._screen_action in _SAVER_FOR_ACTION:
            # Re-arm before returning, not on the next poll. Stopping playback
            # drops the screensaver, and the loop's 0.25 s tick plus the
            # caching teardown was long enough to show the UI again — a
            # visible flash of light at the exact moment the user is being
            # left in the dark. Reported on a Bravia.
            if not xbmc.getCondVisibility("System.ScreenSaverActive"):
                xbmc.executebuiltin("ActivateScreenSaver")
            # Leave the screen dark. The user is asleep; restoring
            # screensaver.mode now would deactivate the screensaver and light
            # the room back up, which is the opposite of what they asked for.
            # The borrowed settings are put back when they come back —
            # see await_wake() — and sleep_state.json stays on disk until then
            # so a Kodi killed overnight still recovers them on next start.
            self._awaiting_wake = True
            # Still armed: player.stop() can complete after this point, and
            # the drop that follows it is what the poll catches.
            self._redarken_pending = True
            self._wake_polls = 0
            xbmc.log(
                "Kotome: screen left dark; settings restore deferred to wake",
                xbmc.LOGINFO,
            )
        else:
            self._restore_saver_settings()
            self._clear_state_file()
        self._reset(keep_awaiting=True)

    def _clear_state_file(self):
        try:
            if os.path.exists(SLEEP_STATE_FILE):
                os.remove(SLEEP_STATE_FILE)
        except OSError:
            pass

    def await_wake(self, playing_again=False):
        """Hold the screen dark after expiry, then hand the settings back.

        Stopping playback deactivates Kodi's screensaver — measured on 21.3:
        System.ScreenSaverActive goes true on activation and false the moment
        Player.Stop lands. So the stop that ends the timer also lights the
        room back up unless the screensaver is put straight back. That is done
        exactly once, on the first poll after expiry.

        After that, the screensaver going *and staying* inactive means the
        user is back, and the borrowed settings are returned. Two things this
        deliberately does not do:

        - it does not re-arm the screensaver repeatedly. A poll that
          reactivates on every tick fights the user for control of their own
          screen, and they cannot win it from a remote.
        - it does not key off getGlobalIdleTime(). Input delivered over
          JSON-RPC does not move that clock, so anything driven by it cannot
          be tested, and an untestable wake condition on a feature that runs
          overnight is not worth having.

        GUI.OnScreensaverDeactivated is not used either: it is never announced
        when screensaver.mode is empty, which is the state most installs are
        in and precisely the state being restored.
        """
        if not self._awaiting_wake:
            return

        saver_up = xbmc.getCondVisibility("System.ScreenSaverActive")

        if self._redarken_pending and not playing_again:
            # First poll after the stop: put the screen back the way the timer
            # promised, then leave it alone.
            self._redarken_pending = False
            if not saver_up:
                xbmc.executebuiltin("ActivateScreenSaver")
            self._wake_polls = 0
            return

        if not playing_again:
            if saver_up:
                self._wake_polls = 0
                return
            # Debounced: one inactive reading can be the tail of the stop.
            self._wake_polls += 1
            if self._wake_polls < WAKE_CONFIRM_POLLS:
                return

        self._restore_saver_settings()
        self._clear_state_file()
        self._awaiting_wake = False
        self._redarken_pending = False
        self._wake_polls = 0
        self._saved_saver_mode = None
        self._saved_saver_audio = None
        xbmc.log("Kotome: user returned — screensaver settings restored", xbmc.LOGINFO)

    def _reset(self, keep_awaiting=False):
        self.active = False
        self.original_volume = None
        self.last_applied_volume = None
        self.user_overrode_volume = False
        self._screen_action_fired = False
        if not (keep_awaiting and self._awaiting_wake):
            self._saved_saver_mode = None
            self._saved_saver_audio = None

    def _restore_saver_settings(self):
        """Give Kodi's screensaver settings back exactly as we found them."""
        if self._saved_saver_mode is not None:
            _set_setting(SETTING_SAVER_MODE, self._saved_saver_mode)
        if self._saved_saver_audio is not None:
            _set_setting(SETTING_SAVER_AUDIO, bool(self._saved_saver_audio))

    def _wake_screen(self):
        if self._screen_action == "screen_off":
            if xbmc.getCondVisibility("System.DPMSActive"):
                xbmc.executebuiltin("ToggleDPMS")
        elif xbmc.getCondVisibility("System.ScreenSaverActive"):
            # 'noop' deactivates the screensaver and does nothing else. Any
            # real input wakes it too, but Select lands on whatever has focus
            # and can answer a dialog nobody knew was open.
            _jsonrpc("Input.ExecuteAction", {"action": "noop"})

    @staticmethod
    def _idle_threshold():
        try:
            return int(ADDON.getSetting("sleep_idle_seconds"))
        except (ValueError, TypeError):
            return 30

    def _maybe_fire_screen_action(self):
        if self._screen_action == "none":
            return
        idle_thresh = self._idle_threshold()
        try:
            idle = xbmc.getGlobalIdleTime()
        except Exception:
            return

        if idle < idle_thresh:
            if self._screen_action_fired:
                # The user came back. Re-arm so the action fires again on the
                # next idle period; the screensaver has already dismissed
                # itself on their input.
                self._screen_action_fired = False
            return

        if self._screen_action_fired:
            return

        if self._screen_action == "screen_off":
            xbmc.executebuiltin("ToggleDPMS")
            self._screen_action_fired = True
            xbmc.log(
                "Kotome: display off via DPMS (idle={})".format(idle), xbmc.LOGINFO
            )
        elif self._screen_action in _SAVER_FOR_ACTION:
            self._activate_screensaver()
            self._screen_action_fired = True
            xbmc.log(
                "Kotome: screensaver activated (idle={}, mode={})".format(
                    idle, self._screen_action
                ),
                xbmc.LOGINFO,
            )

    def _activate_screensaver(self):
        """Fire Kodi's own screensaver, borrowing two settings to do it.

        screensaver.disableforaudio defaults to true, and with it set
        ActivateScreenSaver is a silent no-op during audio playback — measured
        on 21.3: System.ScreenSaverActive stayed false with it on and went true
        with it off, same call either way. An empty screensaver.mode ("None",
        which real installs use) draws nothing, so a named look is substituted
        for the duration of the timer.
        """
        wanted = _SAVER_FOR_ACTION.get(self._screen_action)
        if wanted is None:
            # 'the user's own choice' — but None is not a choice we can show.
            wanted = self._saved_saver_mode or SAVER_BLACK
        _set_setting(SETTING_SAVER_MODE, wanted)
        _set_setting(SETTING_SAVER_AUDIO, False)
        xbmc.executebuiltin("ActivateScreenSaver")

    def _maybe_ramp_volume(self, remaining):
        try:
            ramp = int(ADDON.getSetting("sleep_rampdown_seconds"))
        except (ValueError, TypeError):
            ramp = 30
        if ramp <= 0 or remaining > ramp or self.original_volume is None:
            return
        if self.user_overrode_volume:
            return
        cur = _jsonrpc("Application.GetProperties", {"properties": ["volume"]}).get(
            "volume"
        )
        if (
            self.last_applied_volume is not None
            and cur is not None
            and cur != self.last_applied_volume
        ):
            xbmc.log(
                "Kotome: volume ramp backed off (user adjusted from "
                "{} to {})".format(self.last_applied_volume, cur),
                xbmc.LOGINFO,
            )
            self.user_overrode_volume = True
            return
        target = max(0, int(round(self.original_volume * remaining / ramp)))
        if cur is None or target == cur:
            return
        _jsonrpc("Application.SetVolume", {"volume": target})
        self.last_applied_volume = target


class KotomeMonitor(xbmc.Monitor):
    """Detects addon settings changes and writes new tempo to the shared file."""

    def __init__(self):
        super().__init__()
        self.settings_changed = False

    def onSettingsChanged(self):
        self.settings_changed = True


def set_kotome_properties(win, session_data, player, chapters):
    """Update Kotome-specific window properties during playback."""
    try:
        current_time = player.getTime()
    except Exception:
        current_time = 0

    # Chapter display
    chapter_name = find_chapter(chapters, current_time)
    if chapter_name:
        _set_property(win, "ChapterName", chapter_name)

    # Now playing info from session
    meta = session_data.get("media_metadata", {})
    if meta.get("title"):
        _set_property(win, "NowPlaying.Title", meta["title"])
    if meta.get("author"):
        _set_property(win, "NowPlaying.Author", meta["author"])


def clear_kotome_properties(win):
    for prop in (
        "Kotome.ChapterName",
        "Kotome.NowPlaying.Title",
        "Kotome.NowPlaying.Author",
        "Kotome.SleepTimerRemaining",
    ):
        win.clearProperty(prop)


def run():
    monitor = KotomeMonitor()
    player = xbmc.Player()
    win = xbmcgui.Window(10000)

    sync_interval = 30
    try:
        sync_interval = int(ADDON.getSetting("sync_interval"))
    except (ValueError, TypeError):
        pass

    active_session = None
    last_sync = 0
    client = None
    chapters = []
    last_book_speed_save = 0
    last_active = False
    sleep_controller = SleepModeController()

    # Seed the shared tempo config so speed.py has min/max/step ready even
    # if the user triggers keys before opening playback from Kotome.
    write_config()
    # An addon update restores the zip's <reuselanguageinvoker>true</>, so a
    # user who turned the setting off needs the file rewritten again. Too
    # late for this session — ExtraInfo is already loaded — but the next
    # Kodi start then matches.
    apply_reuse_invoker(notify=False)
    last_reuse = ADDON.getSetting("reuse_language_invoker")

    xbmc.log("Kotome service started", xbmc.LOGINFO)

    # 0.25s poll keeps the resume-seek latency down once the stream is ready,
    # so the user hears as little of the pre-resume audio as possible.
    while not monitor.abortRequested():
        if monitor.waitForAbort(0.25):
            break

        # When the sentinel appears or disappears, refresh a Kotome listing
        # if that's what the user is currently looking at — so the root shows
        # the "Now playing" item without needing a manual re-entry.
        active_now = os.path.exists(ACTIVE_FILE)
        if active_now != last_active:
            folder = xbmc.getInfoLabel("Container.FolderPath") or ""
            if "plugin.audio.kotome" in folder:
                xbmc.executebuiltin("Container.Refresh")
            last_active = active_now

        # Handle settings change — refresh sync interval and tempo config.
        if monitor.settings_changed:
            monitor.settings_changed = False
            try:
                sync_interval = int(ADDON.getSetting("sync_interval"))
            except (ValueError, TypeError):
                pass
            # Refresh shared tempo config so speed.py sees new step/min/max.
            # Speed changes during playback are driven by inputstream.tempo's
            # keyboard/remote shortcuts which write directly to TEMPO_FILE.
            write_config()
            reuse = ADDON.getSetting("reuse_language_invoker")
            if reuse != last_reuse:
                last_reuse = reuse
                apply_reuse_invoker(notify=True)

        if not player.isPlaying():
            if active_session:
                # Playback stopped — close the session
                _close_active_session(client, active_session, "session")
                active_session = None
                client = None
                chapters = []
                clear_session()
                clear_kotome_properties(win)
                _release_sentinel()
            sleep_controller.on_playback_stopped(win)
            sleep_controller.await_wake()
            continue

        # Audio is playing — check if we have a session to track
        sleep_controller.await_wake(playing_again=True)
        session_data = load_session()
        if not session_data:
            continue

        session_id = session_data.get("session_id")
        if not session_id:
            continue

        # New session detected
        if not active_session or active_session.get("session_id") != session_id:
            # Close the previous session before tracking the new one
            if active_session:
                _close_active_session(client, active_session, "previous session")

            active_session = session_data
            chapters = session_data.get("chapters", [])
            last_sync = time.time()
            client = get_client()
            xbmc.log("Kotome: tracking session {}".format(session_id), xbmc.LOGINFO)

        # Update Kotome window properties (chapter, now playing)
        set_kotome_properties(win, active_session, player, chapters)

        # Sleep timer + screensaver swap + volume ramp-down
        sleep_controller.tick(player, win)

        # Save per-book speed periodically (every 10s, if changed)
        now = time.time()
        if (
            ADDON.getSetting("per_book_speed") != "false"
            and now - last_book_speed_save > 10
        ):
            last_book_speed_save = now
            item_id = active_session.get("item_id")
            if item_id:
                current_tempo = read_tempo()
                save_book_speed(item_id, current_tempo)

        # Periodic sync
        if now - last_sync < sync_interval:
            continue

        if client:
            try:
                current_time = player.getTime()
                duration = active_session.get("duration", 0)
                start_time = active_session.get("start_time", 0)
                # Guard: don't overwrite a valid resume position with 0.
                # Can happen if GetTime() hasn't caught up after an initial
                # seek (e.g. m_currentPts not yet set in the inputstream).
                if current_time < 5 and start_time > 30:
                    continue
                listened = now - last_sync
                last_sync = now
                active_session["last_time"] = current_time
                client.sync_session(session_id, current_time, duration, listened)
                xbmc.log(
                    "Kotome: synced {:.0f}s/{:.0f}s".format(current_time, duration),
                    xbmc.LOGINFO,
                )
            except Exception as e:
                xbmc.log("Kotome: sync error: {}".format(e), xbmc.LOGWARNING)

    # Kodi is shutting down — close any active session and restore any
    # in-flight sleep-mode state so the user's screensaver/volume aren't
    # left on the sleep-mode values across a Kodi restart.
    if active_session:
        _close_active_session(client, active_session, "session on shutdown")
        clear_session()
    sleep_controller.on_playback_stopped(win)
    sleep_controller.await_wake(playing_again=True)
    clear_kotome_properties(win)

    xbmc.log("Kotome service stopped", xbmc.LOGINFO)


if __name__ == "__main__":
    run()
