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
import playback
import playing
from playing import jsonrpc as _jsonrpc
from abs_api import ABSClient

ADDON = xbmcaddon.Addon()
PROFILE_DIR = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
SLEEP_FILE = os.path.join(PROFILE_DIR, "sleep_timer")
SLEEP_STATE_FILE = os.path.join(PROFILE_DIR, "sleep_state.json")


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


def _close_active_session(client, session, label="session"):
    """Sync the last second heard, and close the session.

    A session nothing was heard from is closed without a sync: playback
    failed to start, or a Kodi killed mid-book left its file behind. It has
    no position of its own to give the server, and syncing where it started
    would put the bookmark back over a newer one.
    """
    if not (client and session):
        return
    sid = session.get("session_id")
    last = session.get("last_time")
    try:
        if last is None:
            xbmc.log(
                "Kotome: {} {} was never heard; closing it without a sync".format(
                    label, sid
                ),
                xbmc.LOGINFO,
            )
        else:
            client.sync_session(sid, last, session.get("duration", 0), 0)
        client.close_session(sid)
        xbmc.log("Kotome: closed {} {}".format(label, sid), xbmc.LOGINFO)
    except Exception as e:
        xbmc.log("Kotome: error closing {}: {}".format(label, e), xbmc.LOGWARNING)


def _end_session(client, session, win, label="session"):
    """Close the session with ABS, and forget it."""
    _close_active_session(client, session, label)
    playing.clear_session(session["session_id"])
    clear_kotome_properties(win)


def _follow_record(session, saved):
    """Take what main.py rewrites in the session file, when it plays the book
    again from another file in the same session."""
    for key in ("queued", "expect"):
        session[key] = saved.get(key)


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
# renaming one breaks it with no warning and no error.
_PROPERTY_NAMES = (
    "Chapter",
    "ChapterCount",
    "ChapterName",
    "Book.Time",
    "Book.Remaining",
    "NowPlaying.Title",
    "NowPlaying.Author",
    "SleepTimerRemaining",
)


def _set_property(win, name, value):
    win.setProperty("Kotome." + name, value)


def _clear_property(win, name):
    win.clearProperty("Kotome." + name)


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
    """Notices add-on settings changes."""

    def __init__(self):
        super().__init__()
        self.settings_changed = False

    def onSettingsChanged(self):
        self.settings_changed = True


def set_kotome_properties(win, session_data, book_time, tempo):
    """Window properties for the book playing, in book time."""
    # Where the server has no chapter for this point of the book (one library
    # item holding several books, say), none are published, and a skin falls
    # back to Player.Chapter*: the file's own chapters.
    position, chapter = playback.chapter_at(
        session_data.get("chapters") or [], book_time
    )
    if chapter is not None:
        _set_property(win, "ChapterCount", str(len(session_data["chapters"])))
        _set_property(win, "Chapter", str(position))
        _set_property(win, "ChapterName", chapter.get("title", ""))
    else:
        for name in ("ChapterCount", "Chapter", "ChapterName"):
            _clear_property(win, name)

    duration = float(session_data.get("duration") or 0)
    _set_property(win, "Book.Time", playback.clock(book_time))
    if duration > 0:
        _set_property(
            win,
            "Book.Remaining",
            playback.clock((duration - book_time) / (tempo or 1.0)),
        )

    meta = session_data.get("media_metadata", {})
    if meta.get("title"):
        _set_property(win, "NowPlaying.Title", meta["title"])
    if meta.get("author"):
        _set_property(win, "NowPlaying.Author", meta["author"])


def clear_kotome_properties(win):
    for name in _PROPERTY_NAMES:
        _clear_property(win, name)


def run():
    monitor = KotomeMonitor()
    player = playing.KotomePlayer()
    win = xbmcgui.Window(10000)

    sync_interval = 30
    try:
        sync_interval = int(ADDON.getSetting("sync_interval"))
    except (ValueError, TypeError):
        pass

    active_session = None
    last_sync = 0
    client = None
    tempo = playing.TempoKeeper()
    last_tempo_check = 0
    last_now_playing = False
    # When each of the session's files was last heard playing, by track index.
    heard = {}
    sleep_controller = SleepModeController()

    # An addon update restores the zip's <reuselanguageinvoker>true</>, so a
    # user who turned the setting off needs the file rewritten again. Too
    # late for this session — ExtraInfo is already loaded — but the next
    # Kodi start then matches.
    apply_reuse_invoker(notify=False)
    last_reuse = ADDON.getSetting("reuse_language_invoker")

    xbmc.log("Kotome service started", xbmc.LOGINFO)

    while not monitor.abortRequested():
        if monitor.waitForAbort(0.25):
            break

        # When our book starts or stops, refresh a Kotome listing if that's
        # what the user is looking at, so the root shows or drops "Now playing".
        now_playing = bool(win.getProperty("Kotome.NowPlaying.Title"))
        if now_playing != last_now_playing:
            folder = xbmc.getInfoLabel("Container.FolderPath") or ""
            if "plugin.audio.kotome" in folder:
                xbmc.executebuiltin("Container.Refresh")
            last_now_playing = now_playing

        if monitor.settings_changed:
            monitor.settings_changed = False
            try:
                sync_interval = int(ADDON.getSetting("sync_interval"))
            except (ValueError, TypeError):
                pass
            reuse = ADDON.getSetting("reuse_language_invoker")
            if reuse != last_reuse:
                last_reuse = reuse
                apply_reuse_invoker(notify=True)

        now = time.time()
        stopped = player.stop_reason(now)
        if stopped is not None:
            if active_session:
                if stopped == "unknown":
                    continue
                saved = playing.load_session() or {}
                if saved.get("session_id") == active_session["session_id"]:
                    _follow_record(active_session, saved)
                    action = playback.when_stopped(
                        active_session, heard, now, ended=stopped == "ended"
                    )
                else:
                    # another book has been asked for
                    action = "close"
                if action == "wait":
                    continue
                if action == "next":
                    player.ended = False
                    playing.play_next(active_session, active_session["position"])
                    continue
                _end_session(client, active_session, win)
                active_session = None
                client = None
            sleep_controller.on_playback_stopped(win)
            sleep_controller.await_wake()
            continue

        # Something is playing: is it one of our book's files?
        sleep_controller.await_wake(playing_again=True)
        session_data = playing.load_session()
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
            heard = {}
            last_sync = now
            client = get_client()
            tempo.start(session_data)
            xbmc.log("Kotome: tracking session {}".format(session_id), xbmc.LOGINFO)
        else:
            _follow_record(active_session, session_data)

        url, file_time = playing.player_state(player)
        if not url:
            continue
        position, book_time = playback.locate(
            active_session["tracks"], session_id, url, file_time
        )
        if position is None:
            # Something else is playing, or a file asked for has not opened yet.
            if playback.when_stopped(active_session, heard, now) == "wait":
                continue
            xbmc.log("Kotome: other media is playing", xbmc.LOGINFO)
            _end_session(client, active_session, win)
            active_session = None
            client = None
            sleep_controller.on_playback_stopped(win)
            continue
        active_session["position"] = position
        heard[active_session["tracks"][position]["index"]] = now
        # What closing the session syncs, so a stop keeps the last second
        # heard rather than the last periodic sync. Not before the player has
        # reached the position asked for.
        asked = (active_session.get("expect") or {}).get("time", 0)
        reached = playback.reached(book_time, asked)
        if reached:
            active_session["last_time"] = book_time

        if player.av_started:
            player.av_started = False
            player.starting = False
            tempo.apply()
        elif not player.starting and now - last_tempo_check > 1:
            last_tempo_check = now
            tempo.follow()

        set_kotome_properties(win, active_session, book_time, tempo.wanted)

        # Sleep timer + screensaver swap + volume ramp-down
        sleep_controller.tick(player, win)

        # Periodic sync
        if not reached or now - last_sync < sync_interval:
            continue

        if client:
            try:
                duration = active_session.get("duration", 0)
                listened = now - last_sync
                last_sync = now
                client.sync_session(session_id, book_time, duration, listened)
                xbmc.log(
                    "Kotome: synced {:.0f}s/{:.0f}s".format(book_time, duration),
                    xbmc.LOGINFO,
                )
            except Exception as e:
                xbmc.log("Kotome: sync error: {}".format(e), xbmc.LOGWARNING)

    # Kodi is shutting down — close any active session and restore any
    # in-flight sleep-mode state so the user's screensaver/volume aren't
    # left on the sleep-mode values across a Kodi restart.
    if active_session:
        _end_session(client, active_session, win, "session on shutdown")
    sleep_controller.on_playback_stopped(win)
    sleep_controller.await_wake(playing_again=True)
    clear_kotome_properties(win)

    xbmc.log("Kotome service stopped", xbmc.LOGINFO)


if __name__ == "__main__":
    run()
