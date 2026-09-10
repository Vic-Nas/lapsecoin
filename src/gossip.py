"""Outbound block/tx broadcasting over UDP.

Replaces HTTP POST gossip. Uses UDPTransport.send_block() and send_tx().
Dandelion stem/fluff logic is unchanged; only the transport differs.
"""

import logging
import random
import threading
from cachetools import LRUCache

import tx as tx_mod

log = logging.getLogger("ec.gossip")

SEEN_TX_CACHE_SIZE = 50_000
STEM_HOPS_MIN      = 2
STEM_HOPS_MAX      = 8

# Below this many known peers, Dandelion's anonymity set is too small to hide
# a tx's origin anyway (there's nowhere real to blend in), while the stem
# phase still carries a real liveness cost: each hop is a single UDP
# datagram with no retry, so one dropped packet silently kills the relay
# forever with no visible symptom. Not worth paying that cost for privacy
# that isn't there, so skip straight to flooding.
MIN_PEERS_FOR_STEM = STEM_HOPS_MAX


class Gossip:

    def __init__(self, pool, udp):
        self.pool     = pool
        self.udp      = udp
        self._seen_tx = LRUCache(maxsize=SEEN_TX_CACHE_SIZE)
        self._lock    = threading.Lock()

    # ---- Public API (called by Node) ----

    def broadcast_block(self, block):
        """Send a block we built ourselves to every direct peer.

        This is only the first hop. Multi-hop propagation is already
        handled below this layer: peer_udp's MT_BLOCK dispatch calls
        _rebroadcast() on first sight of a msg_id, forwarding to every
        peer except the sender, so a block floods the whole network
        epidemically off this one call. Do not add a second relay on top
        of that -- send_block() mints a fresh msg_id, which defeats the
        transport's msg_id dedup and starts an entirely new flood wave.
        """
        self.udp.send_block(block)

    def relay_tx(self, tx_dict):
        """Start a fresh Dandelion relay for a locally-submitted or fluffed tx.

        Skips the private stem phase entirely below MIN_PEERS_FOR_STEM known
        peers and floods immediately instead -- see MIN_PEERS_FOR_STEM.
        """
        if self.pool.count() < MIN_PEERS_FOR_STEM:
            self.dandelion_send(tx_dict, 0)
            return
        hops = random.randint(STEM_HOPS_MIN, STEM_HOPS_MAX)
        self.dandelion_send(tx_dict, hops)

    def mark_seen(self, h):
        """Mark h as seen. Returns True if already seen, False if new."""
        with self._lock:
            if h in self._seen_tx:
                return True
            self._seen_tx[h] = True
            return False

    # ---- Dandelion stem/fluff ----

    def dandelion_send(self, tx_dict, remaining_hops):
        """Forward tx along the Dandelion stem or fluff it.

        Stem (remaining_hops > 0): send to one random peer with the countdown.
        Skips the seen cache so small networks can complete the stem even with
        only 2 nodes — the countdown bounds any loop.

        Fluff (remaining_hops == 0 or no peers): broadcast to all peers.
        Checks seen cache here to prevent broadcast storms.
        """
        if remaining_hops > 0:
            peer = self.pool.random()
            if peer:
                log.debug("[gossip] dandelion stem  hops_left=%d", remaining_hops)
                self.udp.send_tx(tx_dict, peers=[peer],
                                 remaining_hops=remaining_hops - 1)
                return
        # Fluff: broadcast to all, but only once per tx
        h = tx_mod.tx_hash(tx_dict)
        with self._lock:
            if h in self._seen_tx:
                return
            self._seen_tx[h] = True
        log.debug("[gossip] dandelion fluff")
        self.udp.send_tx(tx_dict, remaining_hops=0)
