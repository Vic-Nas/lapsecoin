"""
Unit tests for peer_udp.py's UDPTransport._dispatch, MT_TX path only.

Regression test for a bug where the propagation phase was dropped on
receive, collapsing every inbound tx to an immediate fluff regardless of
what the sender actually put on the wire, defeating Dandelion's stem
phase between processes. No sockets are opened; _dispatch is called
directly with a hand-built message.
"""

import os
import socket
import sys
import threading
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import peer_udp
from peer_udp import (LAN_DISCOVERY_PORT, MT_GETINFO, MT_INFO, MT_PING, MT_PONG,
                       MT_TX, UDPTransport, _decode, _encode, probe_lan_ports)


def _make_transport(on_tx):
    return UDPTransport(
        port=9999,
        genesis_hash="a" * 64,
        on_block=MagicMock(),
        on_tx=on_tx,
        on_peers=MagicMock(),
        pool=MagicMock(),
    )


def test_dispatch_forwards_the_stem_flag():
    on_tx = MagicMock()
    udp = _make_transport(on_tx)
    tx = {"from": "addr"}
    udp._dispatch(MT_TX, 111, {"tx": tx, "stemming": True}, ("1.2.3.4", 5000))
    on_tx.assert_called_once_with(tx, "1.2.3.4:5000", True)


def test_dispatch_defaults_to_public_when_flag_absent():
    on_tx = MagicMock()
    udp = _make_transport(on_tx)
    tx = {"from": "addr"}
    udp._dispatch(MT_TX, 222, {"tx": tx}, ("1.2.3.4", 5000))
    on_tx.assert_called_once_with(tx, "1.2.3.4:5000", False)


def test_dispatch_dedups_by_msg_id():
    on_tx = MagicMock()
    udp = _make_transport(on_tx)
    tx = {"from": "addr"}
    msg = {"tx": tx, "stemming": False}
    udp._dispatch(MT_TX, 333, msg, ("1.2.3.4", 5000))
    udp._dispatch(MT_TX, 333, msg, ("1.2.3.4", 5000))
    assert on_tx.call_count == 1


# ---------------------------------------------------------------------------
# MT_GETINFO / MT_INFO wallet/version fields, must stay wire-compatible
# with peers not carrying them yet.
# ---------------------------------------------------------------------------

def test_getinfo_response_includes_wallet_and_version():
    """Responding to MT_GETINFO must include our wallet and version
    alongside height/tip."""
    udp = _make_transport(MagicMock())
    udp.set_tip_provider(lambda: (42, "deadbeef", "some.wallet.address", "0.1.1", 4242))
    sent = []
    udp._send_one = lambda msg_type, msg_id, data, target: sent.append((msg_type, data, target))

    udp._dispatch(MT_GETINFO, 1, {"genesis": udp.genesis_hash}, ("1.2.3.4", 5000))

    assert len(sent) == 1
    msg_type, data, target = sent[0]
    assert msg_type == MT_INFO
    assert data == {"genesis": udp.genesis_hash, "height": 42, "work": 4242,
                    "tip_hash": "deadbeef", "wallet": "some.wallet.address",
                    "version": "0.1.1"}


def test_info_reply_captures_wallet_and_version():
    """A well-formed MT_INFO reply's wallet/version fields land in the
    pending result."""
    udp = _make_transport(MagicMock())
    ev = threading.Event()
    with udp._info_lock:
        udp._info_events[7] = ev

    udp._dispatch(MT_INFO, 7, {"height": 10, "tip_hash": "abc",
                              "wallet": "peer.wallet", "version": "0.2.0"},
                  ("1.2.3.4", 5000))

    assert udp._info_results[7] == {"height": 10, "tip_hash": "abc", "work": None,
                                    "wallet": "peer.wallet", "version": "0.2.0"}


def test_info_reply_from_older_peer_without_wallet_or_version_field():
    """An old peer's MT_INFO reply (no wallet/version keys at all) must not
    break. Both just come back empty instead of missing/erroring."""
    udp = _make_transport(MagicMock())
    ev = threading.Event()
    with udp._info_lock:
        udp._info_events[9] = ev

    udp._dispatch(MT_INFO, 9, {"height": 10, "tip_hash": "abc"}, ("1.2.3.4", 5000))

    assert udp._info_results[9] == {"height": 10, "tip_hash": "abc", "work": None,
                                    "wallet": "", "version": ""}


# ---------------------------------------------------------------------------
# LAN auto-admit: a private-source PING or PONG proves direct local
# reachability, so it's admitted on sight instead of needing a manual add.
# ---------------------------------------------------------------------------

def test_ping_from_lan_source_admits_to_pool():
    pool = MagicMock()
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=pool)
    udp._send_one = MagicMock()
    udp._dispatch(MT_PING, 1, {"genesis": udp.genesis_hash}, ("192.168.1.42", 8333))
    pool.add.assert_called_once_with("192.168.1.42:8333", allow_private=True)


def test_pong_from_lan_source_admits_to_pool():
    pool = MagicMock()
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=pool)
    udp._dispatch(MT_PONG, 2, {"observed": "192.168.1.42:8333"}, ("192.168.1.42", 8333))
    pool.add.assert_called_once_with("192.168.1.42:8333", allow_private=True)


def test_ping_from_public_source_does_not_bypass_pool_validation():
    """A public-IP sender still goes through PeerPool.add's own routability
    check (allow_private only makes sense for genuinely private sources)."""
    pool = MagicMock()
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=pool)
    udp._send_one = MagicMock()
    udp._dispatch(MT_PING, 3, {"genesis": udp.genesis_hash}, ("8.8.8.8", 8333))
    pool.add.assert_not_called()


def test_ping_from_loopback_does_not_self_admit():
    pool = MagicMock()
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=pool)
    udp._send_one = MagicMock()
    udp._dispatch(MT_PING, 4, {"genesis": udp.genesis_hash}, ("127.0.0.1", 8333))
    pool.add.assert_not_called()


def test_ping_from_own_private_ip_does_not_self_admit():
    """A cloud instance's own broadcast can loop back to itself over its
    private VPC IP (e.g. AWS's 172.31.x.x behind a public/elastic IP).
    That must not self-admit just because the source happens to be private."""
    pool = MagicMock()
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=pool)
    udp._send_one = MagicMock()
    udp._local_ips = {"172.31.17.210"}
    udp._dispatch(MT_PING, 5, {"genesis": udp.genesis_hash}, ("172.31.17.210", 9999))
    pool.add.assert_not_called()


def test_broadcast_discover_announces_own_port_on_discovery_socket(monkeypatch):
    """The LAN discovery broadcast must carry this node's actual data port
    (self.port) rather than requiring every node to share one port, two
    machines behind the same router commonly use different ports on
    purpose (a router can only forward one external port to one internal
    machine), and this is the whole point of a dedicated discovery port."""
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=MagicMock())
    udp._disc_sock = MagicMock()  # just needs to be truthy
    sent = []
    monkeypatch.setattr(peer_udp, "_broadcast_from_all_interfaces",
                        lambda payload, port: sent.append((payload, port)))

    udp.broadcast_discover()

    assert len(sent) == 1
    payload, port = sent[0]
    assert port == LAN_DISCOVERY_PORT
    decoded = _decode(payload)
    assert decoded == {"type": "announce", "genesis": udp.genesis_hash, "port": 9999}


def test_broadcast_discover_noop_without_discovery_socket():
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=MagicMock())
    udp._disc_sock = None
    udp.broadcast_discover()  # must not raise


def test_disc_announce_from_lan_pings_announced_port():
    """A discovery announcement from a LAN address must trigger a PING to
    the *announced* port, not whatever port the announcement itself arrived
    on, that's what lets two nodes on different data ports find each
    other."""
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=MagicMock())
    pinged = []
    udp.ping = lambda addr: pinged.append(addr)
    payload = _encode({"type": "announce", "genesis": udp.genesis_hash, "port": 8444})

    udp._handle_disc_message(payload, ("192.168.1.50", 8334))

    assert pinged == ["192.168.1.50:8444"]


def test_disc_announce_wrong_genesis_ignored():
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=MagicMock())
    pinged = []
    udp.ping = lambda addr: pinged.append(addr)
    payload = _encode({"type": "announce", "genesis": "different", "port": 8444})

    udp._handle_disc_message(payload, ("192.168.1.50", 8334))

    assert pinged == []


def test_disc_announce_from_own_ip_ignored():
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=MagicMock())
    udp._local_ips = {"192.168.1.50"}
    pinged = []
    udp.ping = lambda addr: pinged.append(addr)
    payload = _encode({"type": "announce", "genesis": udp.genesis_hash, "port": 8444})

    udp._handle_disc_message(payload, ("192.168.1.50", 8334))

    assert pinged == []


def test_disc_announce_from_public_ip_ignored():
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=MagicMock())
    pinged = []
    udp.ping = lambda addr: pinged.append(addr)
    payload = _encode({"type": "announce", "genesis": udp.genesis_hash, "port": 8444})

    udp._handle_disc_message(payload, ("8.8.8.8", 8334))

    assert pinged == []


def test_disc_announce_bad_port_ignored():
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=MagicMock())
    pinged = []
    udp.ping = lambda addr: pinged.append(addr)
    payload = _encode({"type": "announce", "genesis": udp.genesis_hash, "port": "not-a-port"})

    udp._handle_disc_message(payload, ("192.168.1.50", 8334))

    assert pinged == []


def test_disc_probe_gets_announce_reply():
    """A node still picking its own port (probe_lan_ports) sends a bare
    probe with no port of its own, an already-running node must reply
    with its own announce, not try to ping the prober (which has nothing
    listening on a data port yet)."""
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=MagicMock())
    pinged = []
    udp.ping = lambda addr: pinged.append(addr)
    sent = []
    udp._disc_sock = MagicMock()
    udp._disc_sock.sendto = lambda payload, target: sent.append((payload, target))
    payload = _encode({"type": "probe", "genesis": udp.genesis_hash})

    udp._handle_disc_message(payload, ("192.168.1.60", 51234))

    assert pinged == []
    assert len(sent) == 1
    reply_payload, target = sent[0]
    assert target == ("192.168.1.60", 51234)
    assert _decode(reply_payload) == {"type": "announce", "genesis": udp.genesis_hash,
                                      "port": 9999}


def test_disc_probe_from_public_ip_ignored():
    udp = UDPTransport(port=9999, genesis_hash="a" * 64, on_block=MagicMock(),
                       on_tx=MagicMock(), on_peers=MagicMock(), pool=MagicMock())
    udp._disc_sock = MagicMock()
    payload = _encode({"type": "probe", "genesis": udp.genesis_hash})

    udp._handle_disc_message(payload, ("8.8.8.8", 51234))

    udp._disc_sock.sendto.assert_not_called()


# ---------------------------------------------------------------------------
# probe_lan_ports: standalone pre-bind port negotiation.
# ---------------------------------------------------------------------------

def test_probe_lan_ports_collects_matching_replies():
    genesis = "a" * 64
    responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    responder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    responder.bind(("0.0.0.0", 0))
    responder.settimeout(2)

    def respond_once():
        data, sender = responder.recvfrom(2048)
        parsed = _decode(data)
        assert parsed == {"type": "probe", "genesis": genesis}
        reply = _encode({"type": "announce", "genesis": genesis, "port": 8444})
        responder.sendto(reply, sender)

    t = threading.Thread(target=respond_once, daemon=True)
    t.start()
    # Point the probe at our own responder's port instead of the real
    # LAN_DISCOVERY_PORT so the test doesn't depend on that port being free.
    found = probe_lan_ports(genesis, wait=1.0, disc_port=responder.getsockname()[1])
    t.join(timeout=2)
    responder.close()

    assert found == {8444}


def test_probe_lan_ports_survives_a_dropped_first_probe():
    """UDP has no delivery guarantee even on a working LAN, simulate the
    first probe packet vanishing (respond only from the second one
    onward) and confirm the resend still gets a reply within the wait
    window, instead of the whole check silently coming back empty."""
    genesis = "a" * 64
    responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    responder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    responder.bind(("0.0.0.0", 0))
    responder.settimeout(3)

    def respond_from_second_probe():
        responder.recvfrom(2048)  # first probe: dropped, no reply
        data, sender = responder.recvfrom(2048)
        parsed = _decode(data)
        assert parsed == {"type": "probe", "genesis": genesis}
        reply = _encode({"type": "announce", "genesis": genesis, "port": 8444})
        responder.sendto(reply, sender)

    t = threading.Thread(target=respond_from_second_probe, daemon=True)
    t.start()
    found = probe_lan_ports(genesis, wait=1.0, disc_port=responder.getsockname()[1])
    t.join(timeout=3)
    responder.close()

    assert found == {8444}


def test_probe_lan_ports_ignores_wrong_genesis():
    responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    responder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    responder.bind(("0.0.0.0", 0))
    responder.settimeout(2)

    def respond_once():
        data, sender = responder.recvfrom(2048)
        reply = _encode({"type": "announce", "genesis": "other-chain", "port": 8444})
        responder.sendto(reply, sender)

    t = threading.Thread(target=respond_once, daemon=True)
    t.start()
    found = probe_lan_ports("a" * 64, wait=1.0, disc_port=responder.getsockname()[1])
    t.join(timeout=2)
    responder.close()

    assert found == set()


def test_probe_lan_ports_empty_when_nobody_answers():
    found = probe_lan_ports("a" * 64, wait=0.3, disc_port=59991)
    assert found == set()


# ---------------------------------------------------------------------------
# start() bind-retry: a second node process on the same machine shouldn't
# just crash because its requested port is already taken.
# ---------------------------------------------------------------------------

def test_start_falls_back_to_next_free_port_on_collision():
    # Bind to an ephemeral port for a stable, unused starting point.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("0.0.0.0", 0))
    taken_port = probe.getsockname()[1]
    probe.close()

    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("0.0.0.0", taken_port))
    try:
        second = UDPTransport(port=taken_port, genesis_hash="a" * 64, on_block=MagicMock(),
                              on_tx=MagicMock(), on_peers=MagicMock(), pool=MagicMock())
        try:
            second.start()
            assert second.port != taken_port
            assert second.port > taken_port
        finally:
            second.stop()
    finally:
        holder.close()
