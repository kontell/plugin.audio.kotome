"""Book time, tracks and chapters across the files of a book."""

import unittest

import playback

# Three files: 0-600, 600-1500, 1500-1800.
SESSION_TRACKS = [
    {"index": 2, "startOffset": 600, "duration": 900},
    {"index": 1, "startOffset": 0, "duration": 600},
    {"index": 3, "startOffset": 1500, "duration": 300},
]

CHAPTERS = [
    {"start": 0, "end": 400, "title": "One"},
    {"start": 400, "end": 700, "title": "Two"},
    {"start": 700, "end": 1800, "title": "Three"},
]


class TrackTests(unittest.TestCase):
    def setUp(self):
        self.tracks = playback.track_records(SESSION_TRACKS)

    def test_records_are_in_book_order(self):
        self.assertEqual([t["index"] for t in self.tracks], [1, 2, 3])
        self.assertEqual(
            self.tracks[1],
            {"index": 2, "start": 600.0, "duration": 900.0, "mime": ""},
        )

    def test_track_at_a_book_time(self):
        self.assertEqual(playback.track_at(self.tracks, 0), 0)
        self.assertEqual(playback.track_at(self.tracks, 599.9), 0)
        self.assertEqual(playback.track_at(self.tracks, 600), 1)
        self.assertEqual(playback.track_at(self.tracks, 1700), 2)
        self.assertEqual(playback.track_at(self.tracks, 5000), 2)

    def test_start_percent_is_within_the_track(self):
        self.assertAlmostEqual(playback.start_percent(self.tracks[1], 825), 25.0)
        self.assertEqual(playback.start_percent(self.tracks[1], 600), 0.0)
        self.assertEqual(playback.start_percent(self.tracks[0], 0), 0.0)
        self.assertEqual(playback.start_percent(self.tracks[2], 9999), 100.0)

    def test_track_url_round_trip(self):
        url = playback.track_url("https://books.example/", "sess1", 2)
        self.assertEqual(url, "https://books.example/public/session/sess1/track/2")
        self.assertEqual(playback.parse_track_url(url), ("sess1", 2))
        self.assertEqual(
            playback.parse_track_url("http://x/api/items/1/file/2"), (None, None)
        )
        # a file Kodi queued is reported by the plugin path it was queued as
        queued = "plugin://plugin.audio.kotome/?action=play_track&item_id=b&session_id=sess1&index=4"
        self.assertEqual(playback.parse_track_url(queued), ("sess1", 4))
        book = "plugin://plugin.audio.kotome/?action=play_book&item_id=b"
        self.assertEqual(playback.parse_track_url(book), (None, None))
        self.assertEqual(playback.track_by_index(self.tracks, 3), 2)
        self.assertIsNone(playback.track_by_index(self.tracks, 9))

    def test_track_indexes_from_zero_are_kept(self):
        tracks = playback.track_records(
            [
                {"index": 0, "startOffset": 0, "duration": 60},
                {"index": 1, "startOffset": 60, "duration": 60},
            ]
        )
        self.assertEqual([t["index"] for t in tracks], [0, 1])
        url = playback.track_url("https://books.example", "s", 0)
        self.assertEqual(playback.locate(tracks, "s", url, 5.0), (0, 5.0))
        unnumbered = playback.track_records([{"startOffset": 0}, {"startOffset": 60}])
        self.assertEqual([t["index"] for t in unnumbered], [1, 2])

    def test_locate_reads_only_this_sessions_files(self):
        url = playback.track_url("https://books.example", "sess1", 2)
        self.assertEqual(playback.locate(self.tracks, "sess1", url, 25.0), (1, 625.0))
        queued = "plugin://plugin.audio.kotome/?action=play_track&item_id=b&session_id=sess1&index=3"
        self.assertEqual(
            playback.locate(self.tracks, "sess1", queued, 10.0), (2, 1510.0)
        )
        nowhere = (None, None)
        # another session's file, a file that is not a book's, nothing playing
        self.assertEqual(playback.locate(self.tracks, "sess2", url, 25.0), nowhere)
        song = "http://example/song.mp3"
        self.assertEqual(playback.locate(self.tracks, "sess1", song, 25.0), nowhere)
        self.assertEqual(playback.locate(self.tracks, "sess1", None, None), nowhere)
        # a track the session does not have
        gone = playback.track_url("https://books.example", "sess1", 9)
        self.assertEqual(playback.locate(self.tracks, "sess1", gone, 5.0), nowhere)


class ChapterTests(unittest.TestCase):
    def setUp(self):
        self.tracks = playback.track_records(SESSION_TRACKS)

    def test_chapters_move_onto_the_file_clock_and_cross_boundaries(self):
        self.assertEqual(
            playback.track_chapters(CHAPTERS, self.tracks[0]),
            [(0.0, 400.0, "One"), (400.0, 600.0, "Two")],
        )
        self.assertEqual(
            playback.track_chapters(CHAPTERS, self.tracks[1]),
            [(0.0, 100.0, "Two"), (100.0, 900.0, "Three")],
        )
        self.assertEqual(
            playback.track_chapters(CHAPTERS, self.tracks[2]), [(0.0, 300.0, "Three")]
        )

    def test_chapter_at_a_book_time(self):
        self.assertEqual(playback.chapter_at(CHAPTERS, 650)[0], 2)
        self.assertEqual(playback.chapter_at(CHAPTERS, 700)[1]["title"], "Three")
        self.assertEqual(playback.chapter_at(CHAPTERS, 1800), (0, None))
        self.assertEqual(playback.chapter_at([], 10), (0, None))

    def test_clock(self):
        self.assertEqual(playback.clock(59.6), "1:00")
        self.assertEqual(playback.clock(3725), "1:02:05")
        self.assertEqual(playback.clock(-4), "0:00")


class StopTests(unittest.TestCase):
    """What a silence, or another file playing, means for the session."""

    def setUp(self):
        # The listener jumped from file 1 to file 2 at wall-clock 1000.
        self.session = {
            "tracks": playback.track_records(SESSION_TRACKS),
            "queued": True,
            "position": 0,
            "expect": {"index": 2, "time": 650.0, "at": 1000.0},
        }

    def test_waits_for_the_file_asked_for(self):
        self.assertEqual(playback.when_stopped(self.session, {}, 1001.0), "wait")
        # file 1 was still heard just after the jump was asked for
        heard = {1: 1000.2}
        self.assertEqual(playback.when_stopped(self.session, heard, 1001.0), "wait")
        # but not for ever
        self.assertEqual(playback.when_stopped(self.session, heard, 1015.0), "close")

    def test_closes_once_the_file_asked_for_has_played(self):
        # a stop, or other media playing
        heard = {1: 1000.2, 2: 1003.0}
        self.assertEqual(playback.when_stopped(self.session, heard, 1004.0), "close")
        no_ask = dict(self.session, expect=None)
        self.assertEqual(playback.when_stopped(no_ask, heard, 1004.0), "close")

    def test_next_for_an_unqueued_file_that_ended_with_more_to_come(self):
        unqueued = dict(self.session, queued=False, expect=None)
        heard = {1: 990.0}
        self.assertEqual(
            playback.when_stopped(unqueued, heard, 1000.0, ended=True), "next"
        )
        # stopped by the listener
        self.assertEqual(playback.when_stopped(unqueued, heard, 1000.0), "close")
        # the playlist holds the rest
        self.assertEqual(
            playback.when_stopped(self.session, {2: 1003.0}, 1004.0, ended=True),
            "close",
        )
        # the last file
        last = dict(unqueued, position=2)
        self.assertEqual(
            playback.when_stopped(last, heard, 1000.0, ended=True), "close"
        )
        # nothing of the book was heard
        unheard = dict(unqueued, position=None)
        self.assertEqual(
            playback.when_stopped(unheard, {}, 1000.0, ended=True), "close"
        )

    def test_reached(self):
        # the opening of a book resumed half an hour in
        self.assertFalse(playback.reached(0.4, 1800.0))
        self.assertTrue(playback.reached(1800.2, 1800.0))
        # Play from the start
        self.assertTrue(playback.reached(2.0, 0.0))


if __name__ == "__main__":
    unittest.main()
