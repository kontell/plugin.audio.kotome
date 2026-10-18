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


if __name__ == "__main__":
    unittest.main()
