"""Find AudioBookShelf servers on the local network.

Kodi-free on purpose, like :mod:`abs_auth`: the dialog around this lives in
main.py and this module only names hosts and talks to sockets.

AudioBookShelf has no UDP auto-discovery (Jellyfin's port 7359 is a
different protocol). What we can do without the server advertising is scan
the local /24 on ABS's default port and then ask whoever answers GET
/status. The TCP pass is the bulk of the time; HTTP runs only on open
ports, so a quiet /24 does not pay for 254 TLS handshakes.

The window is a retry budget in spirit rather than a patience one: a host
that is up answers the SYN in milliseconds, and one that is not never
will. PROBE_TIMEOUT is therefore short, and the work is concurrent.
"""

import socket
from concurrent.futures import ThreadPoolExecutor, as_completed

# Keep in step with abs_auth.DEFAULT_PORT; this module must not import it
# (abs_auth pulls xbmcaddon).
DEFAULT_PORT = 13378

PROBE_TIMEOUT = 0.4
TCP_WORKERS = 64


def outbound_ipv4():
    """Best-effort LAN address of this box, or '' if we cannot tell.

    Connecting a UDP socket does not send a packet; it just asks the routing
    table which source address would be used. That is the interface the
    /24 scan should cover.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("1.1.1.1", 80))
        return sock.getsockname()[0] or ""
    except OSError:
        return ""
    finally:
        sock.close()


def is_private_ipv4(ip):
    """True for RFC 1918 addresses. A public address is not a LAN to scan."""
    try:
        parts = [int(p) for p in ip.split(".")]
    except (TypeError, ValueError):
        return False
    if len(parts) != 4 or any(p < 0 or p > 255 for p in parts):
        return False
    a, b = parts[0], parts[1]
    if a == 10:
        return True
    if a == 192 and b == 168:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    return False


def slash24_hosts(ip):
    """Every host in the /24 that contains ``ip``, including ``ip`` itself."""
    parts = ip.split(".")
    if len(parts) != 4:
        return []
    prefix = ".".join(parts[:3])
    return ["%s.%d" % (prefix, n) for n in range(1, 255)]


def lan_hosts(local_ip=None):
    """127.0.0.1 plus the local /24 when this box is on a private network."""
    hosts = ["127.0.0.1"]
    ip = local_ip if local_ip is not None else outbound_ipv4()
    if ip and is_private_ipv4(ip):
        for host in slash24_hosts(ip):
            if host not in hosts:
                hosts.append(host)
    return hosts


def tcp_open(host, port=DEFAULT_PORT, timeout=PROBE_TIMEOUT):
    """True if ``host:port`` accepted a TCP connection."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def find_open(
    hosts,
    port=DEFAULT_PORT,
    timeout=PROBE_TIMEOUT,
    workers=TCP_WORKERS,
    connect=tcp_open,
    should_cancel=None,
    on_progress=None,
):
    """Return the hosts where ``connect`` succeeded, in ``hosts`` order.

    ``connect``, ``should_cancel`` and ``on_progress`` are the test seams —
    the unit suite never touches the network.
    """
    if not hosts:
        return []
    open_hosts = []
    worker_count = max(1, min(workers, len(hosts)))
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        pending = {pool.submit(connect, host, port, timeout): host for host in hosts}
        done = 0
        total = len(hosts)
        for future in as_completed(pending):
            done += 1
            if on_progress is not None:
                on_progress(done, total)
            if should_cancel is not None and should_cancel():
                for leftover in pending:
                    leftover.cancel()
                break
            host = pending[future]
            try:
                if future.result():
                    open_hosts.append(host)
            except OSError:
                # A connect implementation that raises rather than returning
                # False is treated as closed; the scan continues.
                pass
    order = {host: index for index, host in enumerate(hosts)}
    open_hosts.sort(key=lambda host: order[host])
    return open_hosts


def is_audiobookshelf(status):
    return isinstance(status, dict) and status.get("app") == "audiobookshelf"


def label_for(address, status=None):
    """``(label, label2)`` for one row of the select dialog.

    Pure, and here rather than in the picker, because Kodistubs'
    ``ListItem.getLabel()`` answers ``''`` — row text is only testable while
    it is a value rather than a widget. ABS /status has no friendly name, so
    the heading is the product plus the version the probe returned.
    """
    version = ""
    if status:
        version = str(status.get("serverVersion") or "")
    name = "AudioBookShelf %s" % version if version else "AudioBookShelf"
    return (name, address)
