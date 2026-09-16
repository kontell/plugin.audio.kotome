"""The General-tab toggle rewrites addon.xml's <reuselanguageinvoker>."""

import os
import unittest

import addonxml

SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<addon id="plugin.audio.kotome">
  <extension point="xbmc.addon.metadata">
    <!-- keep the surrounding comment -->
    <reuselanguageinvoker>true</reuselanguageinvoker>
    <platform>all</platform>
  </extension>
</addon>
"""


class ReuseInvokerTests(unittest.TestCase):
    def test_with_reuse_invoker_flips_only_the_tag(self):
        off = addonxml.with_reuse_invoker(SAMPLE, False)
        self.assertIsNotNone(off)
        self.assertIn("<reuselanguageinvoker>false</reuselanguageinvoker>", off)
        self.assertIn("keep the surrounding comment", off)
        self.assertEqual(addonxml.with_reuse_invoker(off, True), SAMPLE)

    def test_with_reuse_invoker_returns_none_when_the_tag_is_missing(self):
        self.assertIsNone(addonxml.with_reuse_invoker("<addon/>", True))

    def test_apply_writes_and_is_idempotent(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "addon.xml")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(SAMPLE)

            self.assertIs(addonxml.apply(False, path), True)
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("<reuselanguageinvoker>false</reuselanguageinvoker>", text)
            self.assertIs(addonxml.apply(False, path), False)
            self.assertIs(addonxml.apply(True, path), True)
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), SAMPLE)

    def test_apply_returns_none_when_the_file_is_missing(self):
        self.assertIsNone(addonxml.apply(False, os.path.join("no", "such.xml")))

    def test_apply_returns_none_when_the_tag_is_absent(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "addon.xml")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("<addon/>\n")
            self.assertIsNone(addonxml.apply(True, path))
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "<addon/>\n")


if __name__ == "__main__":
    unittest.main()
