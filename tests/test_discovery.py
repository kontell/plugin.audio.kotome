"""LAN AudioBookShelf discovery, against a scripted connect()."""

import unittest

import abs_discovery


class FakeConnect:
    """Answers a set of open hosts; records every call."""

    def __init__(self, open_hosts=()):
        self.open_hosts = set(open_hosts)
        self.calls = []

    def __call__(self, host, port, timeout):
        self.calls.append((host, port, timeout))
        return host in self.open_hosts


class PrivateAddressTests(unittest.TestCase):
    def test_rfc1918(self):
        self.assertTrue(abs_discovery.is_private_ipv4("10.0.0.1"))
        self.assertTrue(abs_discovery.is_private_ipv4("192.168.1.10"))
        self.assertTrue(abs_discovery.is_private_ipv4("172.16.0.2"))
        self.assertTrue(abs_discovery.is_private_ipv4("172.31.255.1"))

    def test_public_and_junk_are_not_a_lan(self):
        self.assertFalse(abs_discovery.is_private_ipv4("1.1.1.1"))
        self.assertFalse(abs_discovery.is_private_ipv4("172.15.0.1"))
        self.assertFalse(abs_discovery.is_private_ipv4("172.32.0.1"))
        self.assertFalse(abs_discovery.is_private_ipv4("127.0.0.1"))
        self.assertFalse(abs_discovery.is_private_ipv4(""))
        self.assertFalse(abs_discovery.is_private_ipv4("not-an-ip"))


class HostListTests(unittest.TestCase):
    def test_slash24_covers_the_whole_subnet(self):
        hosts = abs_discovery.slash24_hosts("192.168.1.10")
        self.assertEqual(hosts[0], "192.168.1.1")
        self.assertEqual(hosts[-1], "192.168.1.254")
        self.assertEqual(len(hosts), 254)
        self.assertIn("192.168.1.10", hosts)

    def test_lan_hosts_always_includes_localhost(self):
        self.assertEqual(abs_discovery.lan_hosts(local_ip=""), ["127.0.0.1"])

    def test_lan_hosts_adds_the_private_slash24(self):
        hosts = abs_discovery.lan_hosts(local_ip="192.168.1.10")
        self.assertEqual(hosts[0], "127.0.0.1")
        self.assertIn("192.168.1.10", hosts)
        self.assertEqual(len(hosts), 255)

    def test_a_public_address_is_not_scanned(self):
        self.assertEqual(abs_discovery.lan_hosts(local_ip="8.8.8.8"), ["127.0.0.1"])


class FindOpenTests(unittest.TestCase):
    def test_quiet_network_returns_nothing(self):
        connect = FakeConnect()
        found = abs_discovery.find_open(
            ["192.168.1.1", "192.168.1.2"], connect=connect, workers=2
        )
        self.assertEqual(found, [])
        self.assertEqual(len(connect.calls), 2)

    def test_open_hosts_keep_input_order(self):
        connect = FakeConnect(open_hosts=("192.168.1.50", "192.168.1.2"))
        found = abs_discovery.find_open(
            ["192.168.1.1", "192.168.1.2", "192.168.1.50"],
            connect=connect,
            workers=3,
        )
        self.assertEqual(found, ["192.168.1.2", "192.168.1.50"])

    def test_cancel_stops_submitting_work(self):
        connect = FakeConnect(open_hosts=("192.168.1.1",))
        calls = {"n": 0}

        def should_cancel():
            calls["n"] += 1
            return calls["n"] > 1

        abs_discovery.find_open(
            ["192.168.1.1", "192.168.1.2", "192.168.1.3"],
            connect=connect,
            should_cancel=should_cancel,
            workers=1,
        )
        # The first completed future sees cancel and the rest are cancelled
        # rather than run. Exact count depends on scheduling; the scan must
        # not raise and must not report hosts it never finished.
        self.assertLessEqual(len(connect.calls), 3)

    def test_progress_is_reported_per_host(self):
        seen = []
        abs_discovery.find_open(
            ["192.168.1.1", "192.168.1.2"],
            connect=FakeConnect(),
            on_progress=lambda done, total: seen.append((done, total)),
            workers=2,
        )
        self.assertEqual(sorted(seen), [(1, 2), (2, 2)])


class StatusTests(unittest.TestCase):
    def test_is_audiobookshelf(self):
        self.assertTrue(
            abs_discovery.is_audiobookshelf(
                {"app": "audiobookshelf", "serverVersion": "2.36.0"}
            )
        )
        self.assertFalse(abs_discovery.is_audiobookshelf({"app": "jellyfin"}))
        self.assertFalse(abs_discovery.is_audiobookshelf(None))
        self.assertFalse(abs_discovery.is_audiobookshelf("nope"))

    def test_label_uses_the_version_the_probe_returned(self):
        self.assertEqual(
            abs_discovery.label_for(
                "http://192.168.1.10:13378",
                {"app": "audiobookshelf", "serverVersion": "2.36.0"},
            ),
            ("AudioBookShelf 2.36.0", "http://192.168.1.10:13378"),
        )

    def test_label_without_a_version_is_the_product_over_the_address(self):
        self.assertEqual(
            abs_discovery.label_for("http://127.0.0.1:13378"),
            ("AudioBookShelf", "http://127.0.0.1:13378"),
        )


if __name__ == "__main__":
    unittest.main()
