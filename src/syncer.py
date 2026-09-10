"""Periodic chain sync over UDP.

Uses UDPTransport.get_info() for lightweight tip comparison and
UDPTransport.request_sync() for fetching chain segments.

Fork choice: most cumulative proven VDF work wins; tip hash breaks ties.
See ChainState.is_better_than().
"""

import logging

log = logging.getLogger("ec.syncer")

FETCH_CHUNK = 50    # blocks per GETSYNC request
# peer_udp.py now has real chunk-level ACK/retransmit for multi-chunk UDP
# messages, so a single dropped datagram no longer silently fails an entire
# page -- the old rationale for keeping this very small (5) no longer
# applies. 50 is chosen the way real sync protocols size a batch: well
# below the hard caps (MAX_SYNC_BLOCKS=500 blocks per request, and
# MAX_CHUNK_TOTAL=2000 chunks / ~2.8MB per reassembled message in
# peer_udp.py), not matched to them -- a block would need to average
# ~56KB for 50 of them to approach that reassembly ceiling even under
# heavy real transaction load (FALCON-512 signatures run large, but not
# that large). Fewer round trips than before for a long initial sync,
# with real recovery underneath if a chunk is still lost along the way.

# Extra attempts before treating an outright timeout/decode-failure (resp is
# None) as authoritative. The UDP transport has no chunk-level retransmission
# (see peer_udp.py), so a single dropped datagram during the binary-search
# fork-point probe previously looked identical to "peer's chain doesn't
# reach this height", and one during a fetch page looked identical to "fetch
# failed" -- either way narrowing the search or aborting the sync on nothing
# more than packet loss. This does not apply to a real response with an
# empty/missing chain field, which is a legitimate answer, not a timeout.
SYNC_REQUEST_RETRIES = 2


class Syncer:

    def __init__(self, pool, udp):
        self.pool = pool
        self.udp  = udp

    def check_and_sync(self, local_chain, apply_fn, info_timeout=8.0):
        """Pick a random peer and sync if they have a better chain.

        Compares by cumulative proven VDF work (tip hash breaks ties).
        Returns True if the chain was updated.

        info_timeout: how long to wait for the initial GETINFO probe.
        Kept short by callers that poll this repeatedly on a tight interval
        (e.g. node.py's mid-VDF-wait polling), so an unresponsive peer
        can't eat a large chunk of that interval every time it's picked --
        the fork-point/fetch phase below still uses its own longer,
        unrelated timeouts since it only runs when there's real work to do.
        """
        peer = self.pool.random()
        if not peer:
            log.debug("[sync] no peers available")
            return False

        info = self.udp.get_info(peer, timeout=info_timeout)
        if info is None:
            log.debug("[sync] info request failed  peer=%s", peer)
            return False

        if not isinstance(info, dict) or "height" not in info:
            log.debug("[sync] unexpected info response  peer=%s", peer)
            return False

        # Cache for display (e.g. the peers page), regardless of whether a
        # sync ends up happening below.
        self.pool.update_info(peer, height=info.get("height"),
                              wallet=info.get("wallet", ""),
                              version=info.get("version", ""))

        remote_height = info["height"]
        local_height  = len(local_chain) - 1
        local_tip     = local_chain[-1]["hash"] if local_chain else ""

        # Only skip if already in sync; otherwise always compare
        if remote_height == local_height and info.get("tip_hash", "") == local_tip:
            log.debug("[sync] already in sync  peer=%s  height=%d", peer, local_height)
            return False

        log.debug("[sync] comparing  peer=%s  remote=%d  local=%d",
                  peer, remote_height, local_height)

        fork_from = self._find_fork_point(peer, local_chain)
        if fork_from is None:
            log.warning("[sync] fork point search failed  peer=%s", peer)
            return False

        log.info("[sync] peer=%s remote=%d local=%d fork_from=%d fetching",
                 peer, remote_height, local_height, fork_from)

        return self._fetch_and_apply(peer, local_chain, fork_from, remote_height, apply_fn)

    def _fetch_and_apply(self, peer, local_chain, fork_from, remote_height, apply_fn):
        """Fetch in FETCH_CHUNK-block pages, applying each page as it
        arrives instead of buffering the whole tail and applying it once at
        the end.

        Two reasons: a node many blocks behind would otherwise sit with an
        unchanged height for the entire fetch, however long that takes,
        then jump straight to the final height in one atomic step -- nothing
        about the transfer is actually all-or-nothing, only its visibility
        was. And a peer that drops mid-fetch now leaves behind whatever
        pages already landed instead of only the single already-existing
        partial-tail fallback below covering that case.

        Applying per page is only cheap because node.py's
        _evaluate_remote_chain has a fast path for the common case (this
        page's chain is a pure extension of the current tip): it builds on
        the already-in-memory ChainState instead of replaying the whole
        chain from genesis on every page.

        Returns True if at least one page was applied.
        """
        applied_any = False
        tail_so_far = []
        h = fork_from
        while h <= remote_height:
            to_h = min(h + FETCH_CHUNK - 1, remote_height)
            resp = self._request_sync_with_retry(peer, from_h=h, to_h=to_h, timeout=30)
            if resp is None:
                log.warning("[sync] fetch page empty  peer=%s  from_h=%d", peer, h)
                break
            page = resp.get("chain") if isinstance(resp, dict) else None
            if not isinstance(page, list) or not page:
                log.warning("[sync] fetch page empty  peer=%s  from_h=%d", peer, h)
                break

            tail_so_far += page
            full_chain = local_chain[:fork_from] + tail_so_far
            if not apply_fn(full_chain):
                log.warning("[sync] page rejected  peer=%s  from_h=%d", peer, h)
                break
            applied_any = True

            if len(page) < FETCH_CHUNK:
                break
            h += FETCH_CHUNK
        return applied_any

    def _request_sync_with_retry(self, peer, from_h, to_h, timeout):
        """request_sync, retrying a bare timeout/decode-failure a few times
        before giving up. See SYNC_REQUEST_RETRIES for why."""
        for _attempt in range(SYNC_REQUEST_RETRIES + 1):
            resp = self.udp.request_sync(peer, from_h=from_h, to_h=to_h, timeout=timeout)
            if resp is not None:
                return resp
        return None

    def _find_fork_point(self, peer, local_chain):
        """Binary search for common ancestor. O(log n) round trips."""
        lo, hi = 0, len(local_chain) - 1
        result = None

        while lo <= hi:
            mid = (lo + hi) // 2
            local_hash = local_chain[mid]["hash"]

            resp = self._request_sync_with_retry(peer, from_h=mid, to_h=mid, timeout=10)
            page = resp.get("chain") if isinstance(resp, dict) else None
            if not isinstance(page, list) or not page:
                # Peer doesn't have this height; their chain is shorter, search lower.
                hi = mid - 1
                continue

            if page[0].get("hash") == local_hash:
                result = mid
                lo = mid + 1
            else:
                hi = mid - 1

        return (result + 1) if result is not None else 0
