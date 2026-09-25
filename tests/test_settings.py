"""Settings buttons must name a route the plugin actually handles."""

import os
import re
import unittest
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _router_actions():
    with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as handle:
        text = handle.read()
    return set(re.findall(r'action == "([^"]+)"', text))


class SettingsButtonTests(unittest.TestCase):
    def test_every_action_button_names_a_registered_route(self):
        tree = ET.parse(os.path.join(ROOT, "resources", "settings.xml"))
        actions = _router_actions()
        missing = []
        for setting in tree.findall(".//setting"):
            data = setting.find("control/data")
            if data is None or not (data.text or "").strip():
                continue
            match = re.search(r"[?&]action=([^&)]+)", data.text)
            if match is None:
                missing.append("%s: %s has no action=" % (setting.get("id"), data.text))
                continue
            action = match.group(1)
            if action not in actions:
                missing.append(
                    "%s: action=%s is not a router arm" % (setting.get("id"), action)
                )
        self.assertEqual(missing, [])

    def test_find_servers_is_logged_out_only_and_closes_the_dialog(self):
        tree = ET.parse(os.path.join(ROOT, "resources", "settings.xml"))
        setting = tree.find(".//setting[@id='find_servers']")
        self.assertIsNotNone(setting)
        visible = setting.find("dependencies/dependency[@type='visible']/condition")
        self.assertEqual(visible.get("setting"), "logged_in")
        self.assertEqual(visible.get("operator"), "is")
        self.assertEqual(visible.text, "false")
        close = setting.find("control/close")
        self.assertIsNotNone(close)
        self.assertEqual(close.text, "true")

    def test_reuse_language_invoker_defaults_on(self):
        tree = ET.parse(os.path.join(ROOT, "resources", "settings.xml"))
        setting = tree.find(".//setting[@id='reuse_language_invoker']")
        self.assertIsNotNone(setting)
        self.assertEqual(setting.find("default").text, "true")
        self.assertEqual(setting.find("control").get("type"), "toggle")
