"""Block cycle orchestrator.

One cycle:
  1. drain queue          inbound txs and blocks from net_in_q
  2. sync                pull a better chain from a random peer
  3. vdf.evaluate()      blocks ~120s, re-checking sync every
                         SYNC_POLL_INTERVAL_SECONDS while waiting
  4. assemble + broadcast
  5. drain queue (5s)    collect peer blocks
  6. pick winner         first valid peer block received, else own candidate
  7. commit              swap ChainState, persist, publish view

Flask threads read node.view (a NodeView snapshot). The node loop is the
sole writer; every mutation publishes a new snapshot atomically.
"""

import collections
import json
import logging
import queue
import statistics
import secrets as _secrets
import state as state_mod
import threading
import time

import block as block_mod
import crypto
import mempool as mempool_mod
import tx as tx_mod
import vdf as vdf_mod
from chainstate import ChainState
from params import DB_PATH
from storage import Storage

log = logging.getLogger("ec.node")
_rng = _secrets.SystemRandom()

SYNC_EVERY_N_CYCLES = 1   # check every cycle; forks are common at 2-min blocks

# How often to re-check for a better peer chain *during* the ~120-200s VDF
# wait, not just once at cycle start. Without this, a node that's badly
# behind can only close its gap once per full mining cycle -- a lagging
# peer would need many multi-minute cycles to catch up even though the
# actual data transfer takes a couple of seconds. This is still called
# from the single node-loop thread (never a separate thread): _drain_queue
# and friends assert they run on that thread, and mempool/state are
# documented single-writer, so interleaving more checks into the existing
# wait loop is safe where spawning a real background thread would not be.
SYNC_POLL_INTERVAL_SECONDS = 10

# Timeout for the mid-wait polls' initial GETINFO probe. Deliberately much
# shorter than UDPTransport.get_info's own 8s default: this probe repeats
# roughly every SYNC_POLL_INTERVAL_SECONDS for the whole ~120-200s wait, and
# a peer that's gone unresponsive (but hasn't yet been struck/evicted) would
# otherwise be able to eat most of that responsive-wait budget, one 8s block
# at a time, on the single node-loop thread this all runs on. In the common
# case (peer alive, already in sync) the real round trip is milliseconds, so
# this only matters for the failure case it's meant to bound.
SYNC_POLL_INFO_TIMEOUT_SECONDS = 2.0

# Recent-state cache: lets a shallow reorg resume from an already-computed
# state instead of replaying the whole chain from genesis (see
# _resume_point). Routine reorgs here are shallow -- a lost race resolves
# within a block or two, ties are the common case, not sustained multi-
# height divergence (see the whitepaper's section on the VDF lottery).
# Anything deeper than this window falls back to a full replay, which is
# also the right conservative behavior for what would be a genuinely
# abnormal, out-of-scope event (a sustained partition or majority
# attacker) -- not something worth building fast-path machinery for.
RECENT_STATE_CACHE_SIZE = 20


# ---------------------------------------------------------------------------
# Tail validation (pure, no node state touched)
# ---------------------------------------------------------------------------

def _validate_tail(tail, prefix):
    """Validate new blocks against a trusted prefix, building the resulting
    ChainState along the way. Pure: nothing is mutated.

    Returns (True, None, cs) or (False, error_string, None). The caller
    reuses the returned cs directly rather than deriving it a second time
    (e.g. via a separate ChainState.from_chain(prefix + tail) call), which
    would silently redo this same replay.
    """
    cs = ChainState.from_chain(prefix) if prefix else ChainState.from_genesis()
    for blk in tail:
        ok, err, cs = cs.validate_and_apply(blk)
        if not ok:
            return False, f"invalid block at {blk.get('height')}: {err}", None
    return True, None, cs


# ---------------------------------------------------------------------------
# NodeView: read-only snapshot for Flask threads
# ---------------------------------------------------------------------------

class NodeView:
    """Immutable snapshot of node state. Published after every block commit.
    Flask reads node.view; one reference swap, GIL-atomic, no lock needed.
    """
    __slots__ = ("chain", "height", "tip", "genesis_hash", "state")

    def __init__(self, cs):
        self.chain        = cs.chain
        self.tip          = cs.tip
        self.height       = cs.height
        self.genesis_hash = cs.genesis_hash
        self.state        = cs.state.snapshot()


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

class Node:

    def __init__(self, keyfile, public_key, gossip, syncer, pool, net_in_q,
                 db_path=None):
        self.keyfile      = keyfile
        self.pk           = public_key
        self.pk_hex       = public_key.hex()
        self.addr         = crypto.public_key_to_address(public_key)
        self.gossip       = gossip
        self.syncer       = syncer
        self.pool         = pool
        self.net_in_q     = net_in_q
        self.mempool      = mempool_mod.Mempool()
        self.storage      = Storage(db_path or DB_PATH)
        self.running      = False
        self._kek         = None
        self._loop_thread = None
        self._cycle_count = 0
        # tx_hash -> monotonic time of the last (re)relay, for txs this node
        # itself originated. The private stem hop is a single UDP datagram
        # with no retry (see gossip.py), so a dropped packet silently kills
        # propagation with no visible symptom until the tx expires from the
        # mempool unconfirmed. _retry_stuck_local_txs() re-floods our own
        # submissions that are still pending after RELAY_RETRY_SECONDS.
        self._local_pending = {}

        # height -> (State snapshot, cumulative_iterations) for the last
        # RECENT_STATE_CACHE_SIZE heights this node has actually committed.
        # Refreshed on every commit (_commit and apply_better_chain), stale
        # entries above a reorg's fork point purged there too. Not
        # persisted across restarts -- it exists to avoid replaying a
        # shallow reorg from genesis, and self-refills within a few
        # cycles of normal running regardless; an empty cache right after
        # startup just means the fallback (full replay) applies until it
        # does. See RECENT_STATE_CACHE_SIZE above for why this stays small.
        self._recent_states = collections.OrderedDict()

        # Real wall-clock seconds this node itself spent on its last 30 VDF
        # evaluations -- recorded the moment each one finishes, regardless of
        # whether the resulting candidate goes on to win its fork race. The
        # chain only ever holds whichever block won each height, so deriving
        # "this machine's build time" from chain timestamps would silently
        # drop every attempt that lost a race and skew toward other builders'
        # numbers entirely. This is local, in-memory, per-node knowledge --
        # nothing else has it, so it can't be reconstructed from chain data.
        self._own_build_seconds = collections.deque(maxlen=30)
        self._load_own_build_seconds()

        self.cs   = self._load_cs()
        self.view = NodeView(self.cs)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    _OWN_BUILD_SECONDS_META_KEY = "own_build_seconds"

    def _load_own_build_seconds(self):
        """Restore _own_build_seconds across restarts, so the odds page and
        own_block_time_ratio() don't sit empty for up to 30 cycles (an hour)
        after every restart. Best-effort: any parse failure just starts
        fresh, same as a node that's never built before."""
        raw = self.storage.get_meta(self._OWN_BUILD_SECONDS_META_KEY)
        if not raw:
            return
        try:
            self._own_build_seconds.extend(json.loads(raw))
        except (json.JSONDecodeError, TypeError, ValueError):
            log.warning("[startup] discarding unreadable own_build_seconds meta")

    def _save_own_build_seconds(self):
        self.storage.set_meta(self._OWN_BUILD_SECONDS_META_KEY,
                              json.dumps(list(self._own_build_seconds)))

    def _load_cs(self):
        """Load or create ChainState from storage."""
        stored = self.storage.load_all_blocks()
        if not stored:
            cs = ChainState.from_genesis()
            self.storage.save_block(cs.chain[0])
            log.info("[startup] genesis created")
            return cs

        # Backfill tx_bytes for blocks from older databases.
        for blk in stored:
            if "tx_bytes" not in blk:
                blk["tx_bytes"] = sum(tx_mod.tx_size(t)
                                       for t in blk.get("transactions", []))

        if self.storage.state_exists():
            s = state_mod.State.from_snapshot(*self.storage.load_state())
            cs = ChainState.from_storage(stored, s)
        else:
            cs = ChainState.from_chain(stored)

        log.info("[startup] chain loaded  height=%d  tip=%s",
                 cs.height, cs.tip["hash"][:12])
        return cs

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def is_signing_active(self):
        return self._kek is not None

    def mark_tx_seen(self, tx_hash):
        return self.gossip.mark_seen(tx_hash)

    def own_vdf_median(self):
        """This node's own median real VDF build time, over its last 30
        actual attempts (own_build_seconds -- see that field's docstring).
        None until this node has completed at least one build."""
        if not self._own_build_seconds:
            return None
        return statistics.median(self._own_build_seconds)

    def own_block_time_ratio(self):
        """This node's own median VDF build time as a ratio of the chain's
        own recent median block-to-block time (block_mod's
        BLOCK_TIME_MEDIAN_WINDOW, whoever actually built each of those
        blocks). Above 1 means this node is running slower than the
        network's recent real pace. None until this node has completed a
        build, or the chain has too few blocks for a chain-side median."""
        own_median = self.own_vdf_median()
        if own_median is None:
            return None
        chain = self.view.chain
        stats = block_mod.block_time_stats(chain, len(chain) - 1)
        if stats is None or not stats["median"]:
            return None
        return own_median / stats["median"]

    def get_info(self):
        v = self.view
        return {
            "height":       v.height,
            "tip_hash":     v.tip["hash"],
            "genesis_hash": v.genesis_hash,
            "mempool_size": self.mempool.size(),
            "address":      self.addr,
            "peer_count":   self.pool.count(),
            "total_minted": v.state.total_minted,
            "can_mint":     v.state.compute_can_mint(),
            "block_reward": v.state.compute_block_reward(),
            "block_time_ratio": self.own_block_time_ratio(),
        }

    def start(self, kek):
        self._kek         = kek
        self.running      = True
        self._loop_thread = threading.current_thread()
        log.info("[startup] node ready  addr=%s", self.addr)
        try:
            while self.running:
                try:
                    self._run_cycle()
                except Exception:
                    log.exception("[cycle] unhandled error, sleeping 1s")
                    time.sleep(1)
        finally:
            self._kek = None

    def stop(self):
        self.running = False

    def _validate_for_mempool(self, tx_dict):
        """Validate tx_dict against confirmed state plus this sender's
        already-pending mempool txs. Shared by submit_tx and
        _handle_inbound_tx so both acceptance paths agree."""
        probe = self.mempool.probe_state_for(tx_dict.get("from"), self.cs.state)
        return tx_mod.validate(tx_dict, probe)

    def submit_tx(self, tx_dict):
        """Validate and add a tx. Node loop thread only."""
        assert threading.current_thread() is self._loop_thread
        ok, err = self._validate_for_mempool(tx_dict)
        if not ok:
            log.debug("[tx] rejected  reason=%s  from=%s",
                      err, tx_dict.get("from", "?")[:24])
            return False, err
        ok, h = self.mempool.add(tx_dict)
        if not ok:
            log.debug("[tx] mempool add failed  reason=%s  from=%s", h,
                      tx_dict.get("from", "?")[:24])
            return False, h
        self.gossip.relay_tx(tx_dict)
        self._local_pending[h] = time.monotonic()
        log.info("[tx] accepted  hash=%s  from=%s", h[:12],
                 tx_dict.get("from", "?")[:24])
        return True, h

    RELAY_RETRY_SECONDS = 180  # ~1.5x the VDF block target

    def _retry_stuck_local_txs(self):
        """Re-flood (skipping the stem phase) any tx we originated that's
        still pending after RELAY_RETRY_SECONDS -- see _local_pending."""
        now = time.monotonic()
        for h in list(self._local_pending):
            tx_dict = self.mempool.get(h)
            if tx_dict is None:
                del self._local_pending[h]  # confirmed or pruned
                continue
            if now - self._local_pending[h] >= self.RELAY_RETRY_SECONDS:
                log.info("[tx] re-flooding stuck local tx  hash=%s", h[:12])
                # Straight to the UDP layer, bypassing gossip's seen-tx
                # dedup cache -- the first send already marked this hash
                # seen (that's what stopped it looping as a fluff storm),
                # which would otherwise make dandelion_send() silently
                # no-op on exactly the resend we're trying to force here.
                self.gossip.udp.send_tx(tx_dict, remaining_hops=0)
                self._local_pending[h] = now

    def submit_tx_from_api(self, tx_dict, timeout=5):
        """Thread-safe bridge: enqueue tx, block until the loop replies."""
        reply = queue.Queue(maxsize=1)
        self.net_in_q.put({"type": "submit_tx", "tx": tx_dict, "reply": reply})
        try:
            return reply.get(timeout=timeout)
        except queue.Empty:
            return False, "node busy (timeout)"

    def build_and_sign_tx(self, to_outputs, fee=0, passphrase=None):
        """Build, sign, and return a plaintext transaction from this node's
        own address. fee is sender-chosen (default 0; callers building a
        wallet UI should let the user pick a competitive fee)."""
        if not passphrase:
            raise ValueError("passphrase is required to sign a transaction")
        kek = crypto.derive_kek(self.keyfile, passphrase)
        try:
            return self._build_and_sign_tx_with_kek(to_outputs, fee, kek)
        finally:
            del kek

    def build_and_sign_tx_internal(self, to_outputs, fee=0):
        """Same as build_and_sign_tx, but for background node-internal
        callers (e.g. the uptime rewarder) that run inside this same
        process while the node is up -- reuses the kek already resident in
        memory from start() instead of asking for the passphrase again,
        since re-deriving it would need the plaintext passphrase this node
        never retains past startup. Raises if the node isn't running
        (is_signing_active() false), same failure mode as a missing
        passphrase would give the passphrase-based path."""
        if self._kek is None:
            raise ValueError("node is not running (no signing key resident)")
        return self._build_and_sign_tx_with_kek(to_outputs, fee, self._kek)

    def _build_and_sign_tx_with_kek(self, to_outputs, fee, kek):
        v         = self.view
        committed = v.state.get_nonce(self.addr)
        pending   = self.mempool.pending_nonce(self.addr)
        nonce     = max(committed, pending) + 1
        sk = crypto.decrypt_secret_key(self.keyfile, kek=kek)
        t  = tx_mod.create(self.addr, self.pk_hex, to_outputs, nonce, fee, sk)
        del sk
        return t, fee

    # ------------------------------------------------------------------
    # Block cycle
    # ------------------------------------------------------------------

    def _run_cycle(self):
        self._cycle_count += 1
        self._drain_queue()
        self._retry_stuck_local_txs()

        if self._cycle_count % SYNC_EVERY_N_CYCLES == 0:
            self.syncer.check_and_sync(
                self.cs.chain,
                lambda chain: self.apply_better_chain(chain)[0],
            )
        cs = self.cs   # local alias; can change under sync
        pruned = self.mempool.prune_stale(cs.state)
        log.info("[vdf] starting height=%d  tip=%s  peers=%d  mempool=%d  pruned=%d",
                 cs.height + 1, cs.tip["hash"][:12],
                 self.pool.count(), self.mempool.size(), len(pruned))

        # Run VDF in a background thread so the node loop stays responsive
        # to tx submissions and peer messages during the ~120s evaluation.
        import concurrent.futures as _cf
        accumulated_blocks = []
        iterations = block_mod.get_vdf_iterations(cs.chain)
        with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
            _fut = _pool.submit(
                vdf_mod.evaluate,
                block_mod.vdf_challenge(cs.tip["hash"], self.addr), iterations)
            last_sync_check = time.monotonic()
            while not _fut.done():
                accumulated_blocks += self._drain_queue(timeout=1)
                # Re-check for a better peer chain periodically instead of only
                # once at cycle start, so a lagging node converges in roughly
                # this interval rather than waiting a full mining cycle per
                # attempt. Still runs on this same thread -- see
                # SYNC_POLL_INTERVAL_SECONDS's comment for why that matters.
                if time.monotonic() - last_sync_check >= SYNC_POLL_INTERVAL_SECONDS:
                    self.syncer.check_and_sync(
                        self.cs.chain,
                        lambda chain: self.apply_better_chain(chain)[0],
                        info_timeout=SYNC_POLL_INFO_TIMEOUT_SECONDS,
                    )
                    last_sync_check = time.monotonic()
            vdf_out, vdf_proof, vdf_seconds = _fut.result()
        log.info("[vdf] proof ready  height=%d  seconds=%.1f  iterations=%d",
                 cs.height + 1, vdf_seconds, iterations)
        self._own_build_seconds.append(vdf_seconds)
        self._save_own_build_seconds()

        if self.cs is not cs:
            # A mid-wait sync check adopted a better chain out from under us.
            # The VDF we just computed was for cs.tip, which is no longer our
            # tip -- apply_block() trusts previous_hash without re-checking
            # it, so committing this candidate would silently splice a block
            # onto the wrong parent. Discard it; the next cycle starts fresh
            # against the new tip. The sunk VDF time isn't recoverable (the
            # computation itself can't be cancelled or reused), same as any
            # other lost fork race.
            log.info("[vdf] tip changed during VDF computation (adopted a "
                     "better chain mid-cycle); discarding in-flight candidate")
            return

        candidate = block_mod.assemble(cs.tip, self.mempool.all_txs(), self.addr, iterations)
        candidate["vdf_output"]    = vdf_out
        candidate["vdf_proof"]     = vdf_proof
        candidate["vdf_iterations"] = iterations
        candidate["hash"]          = block_mod.block_hash(candidate)
        ok, err = block_mod.validate(candidate, cs.state.snapshot(), cs.chain)
        if not ok:
            log.error("[vdf] self-produced block failed validation: %s", err)
            return
        self.gossip.broadcast_block(candidate)

        # Drain anything that arrived just as VDF completed, then pick winner.
        # All peer candidates should already be in accumulated_blocks since VDFs
        # take roughly the same time. _drain_queue() with no timeout flushes
        # whatever is already in the queue without blocking.
        peer_blocks = accumulated_blocks + self._drain_queue()
        # Pass cs explicitly; self.cs may have advanced during drain if syncer fired.
        winner, relay = self._pick_winner(cs, candidate, peer_blocks)
        if winner is None:
            return
        self._commit(winner, relay=relay)

    def _pick_winner(self, cs, candidate, peer_blocks):
        """Return (best_block, relay). relay=True means it came from a peer.
        Returns (None, False) if the candidate is stale (tip changed under sync).

        cs: the ChainState candidate was built against; passed explicitly so
        this method is immune to self.cs advancing during the drain window.
        """
        tip = cs.tip

        valid_peers = []
        for blk in peer_blocks:
            if blk.get("height") != tip["height"] + 1:
                continue
            if blk.get("previous_hash") != tip["hash"]:
                continue
            probe = cs.state.snapshot()
            ok, err = block_mod.validate(blk, probe, cs.chain)
            if not ok:
                log.debug("[vdf] peer block rejected: %s", err)
                continue
            log.debug("[vdf] peer block accepted  height=%d  hash=%s  builder=%s  tx=%d",
                      blk["height"], blk["hash"][:12],
                      (blk.get("builder") or "")[:24], len(blk.get("transactions", [])))
            valid_peers.append(blk)

        if candidate.get("previous_hash") != tip["hash"]:
            log.warning("[vdf] candidate stale (tip advanced during drain), skipping cycle")
            return None, False

        # Among all equally-valid same-height candidates (all proving the
        # same required iterations), the lowest vdf_output wins -- the same
        # rule ChainState.is_better_than uses, so a node's own immediate
        # pick can't diverge from what syncer would settle on anyway.
        winner   = min([candidate] + valid_peers, key=block_mod.tie_break_key)
        is_peer  = winner is not candidate
        log.info("[vdf] winner  hash=%s  peer=%s  candidates=%d  peer_candidates=%d",
                 winner["hash"][:12], is_peer, len(valid_peers) + 1, len(valid_peers))
        return winner, is_peer

    def _commit(self, blk, relay=False):
        """Append a validated block: update ChainState, persist, publish view."""
        confirmed = {tx_mod.tx_hash(t) for t in blk.get("transactions", [])}
        self.cs = self.cs.apply_block(blk)
        self._remember_state(self.cs)
        self.storage.save_block_and_state(blk, self.cs.state)
        self.mempool.remove_many(confirmed)
        self.view = NodeView(self.cs)

        if relay:
            # Peer-won block: broadcast it now. Our own candidate was already
            # broadcast in _run_cycle before the drain window; relay=False there.
            try:
                self.gossip.broadcast_block(blk)
            except Exception:
                log.exception("[commit] relay broadcast failed height=%d", blk["height"])

        log.info("[commit] height=%d  hash=%s  tx=%d  builder=%s",
                 blk["height"], blk["hash"][:12], len(blk["transactions"]),
                 (blk.get("builder") or "")[:24])

    # ------------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------------

    def _drain_queue(self, timeout=0):
        """Drain net_in_q. Returns list of inbound block dicts."""
        assert threading.current_thread() is self._loop_thread
        blocks = []
        try:
            msg = self.net_in_q.get(block=timeout > 0, timeout=timeout or None)
            self._handle(msg, blocks)
        except queue.Empty:
            return blocks
        while True:
            try:
                self._handle(self.net_in_q.get_nowait(), blocks)
            except queue.Empty:
                break
        return blocks

    def _handle(self, msg, block_out):
        """Dispatch one queue message."""
        t = msg.get("type")
        if t == "block":
            block_out.append(msg["block"])
        elif t == "submit_tx":
            msg["reply"].put(self.submit_tx(msg["tx"]))
        elif t == "tx":
            self._handle_inbound_tx(msg)

    def _handle_inbound_tx(self, msg):
        """Route an inbound tx message.

        Stem txs (Dandelion relay) are forwarded without validation; we are
        not the ultimate recipient, just a relay node. Fluff txs are validated
        and added to the mempool if new and valid.
        """
        tx_dict   = msg["tx"]
        sender    = tx_dict.get("from", "?")[:24]
        remaining = msg.get("remaining_hops", 0)
        if msg.get("relay_type") == "tx_stem" and remaining > 0:
            log.debug("[tx] stem relay  hops_remaining=%d  from=%s", remaining, sender)
            self.gossip.dandelion_send(tx_dict, remaining)
            return
        ok, err = self._validate_for_mempool(tx_dict)
        if not ok:
            log.debug("[tx] inbound rejected  reason=%s  from=%s", err, sender)
            return
        added, h_or_err = self.mempool.add(tx_dict)
        if added:
            log.debug("[tx] inbound accepted  hash=%s  from=%s", h_or_err[:12], sender)
            # Continue the fluff, never re-stem: this tx already reached the
            # public phase (either it arrived tagged tx_fluff, or as the
            # stem's last hop -- see the tx_stem branch above), so privacy
            # is already spent and there's nothing left to gain from a fresh
            # private stem here. relay_tx() would re-roll stem-vs-flood from
            # this node's own peer count, which on a well-connected node
            # (>= MIN_PEERS_FOR_STEM) silently turns one flood step into
            # another single-hop, zero-retry private relay -- exactly the
            # failure mode this is meant to avoid. dandelion_send(tx, 0)
            # floods to all peers (deduped via the seen-tx cache).
            self.gossip.dandelion_send(tx_dict, 0)
        else:
            log.debug("[tx] inbound duplicate  from=%s", sender)

    # ------------------------------------------------------------------
    # Chain sync / reorg
    # ------------------------------------------------------------------

    def _remember_state(self, cs):
        """Cache (state, cumulative_iterations) at cs.height, so a later
        shallow reorg back to this point can resume without a full replay.
        Bounded to RECENT_STATE_CACHE_SIZE; see that constant for why."""
        self._recent_states[cs.height] = (cs.state.snapshot(), cs.cumulative_iterations)
        self._recent_states.move_to_end(cs.height)
        while len(self._recent_states) > RECENT_STATE_CACHE_SIZE:
            self._recent_states.popitem(last=False)

    def _forget_states_from(self, height):
        """Purge cached entries at or above `height`: a reorg just replaced
        whatever blocks used to be there, so a *later* reorg landing on one
        of those heights must not reuse state that belonged to the now-
        abandoned branch."""
        for h in [h for h in self._recent_states if h >= height]:
            del self._recent_states[h]

    def _resume_point(self, fork_point, remote_chain):
        """A ChainState to resume validation from at fork_point, avoiding a
        full genesis replay, when one is cheaply available:
          - a pure extension (fork_point == len(self.cs.chain)): self.cs
            itself, already fully built and sitting in memory.
          - a shallow reorg: a cached recent state (see _remember_state).
        Returns None if neither applies (a reorg deeper than the cache, or
        shortly after a restart before the cache has refilled) -- callers
        fall back to a full replay in that case, which is also the right
        conservative behavior for what would be a genuinely abnormal, deep
        divergence (see RECENT_STATE_CACHE_SIZE).
        """
        if fork_point == len(self.cs.chain):
            return self.cs
        resume_height = fork_point - 1
        if resume_height == 0:
            # State right after genesis is always trivially known (empty
            # balances, zero iterations -- genesis carries no transactions
            # and contributes nothing to cumulative_iterations), no need
            # to consult the cache for this one.
            return ChainState(remote_chain[:fork_point], state_mod.State(), 0)
        cached = self._recent_states.get(resume_height)
        if cached is None:
            return None
        base_state, base_iterations = cached
        return ChainState(remote_chain[:fork_point], base_state.snapshot(), base_iterations)

    def _evaluate_remote_chain(self, remote_chain):
        """Pure evaluation of a candidate remote chain. No committed state
        is mutated (the _recent_states cache is only ever read here).

        Returns (ok, err, fork_point, tail, remote_cs) on success,
        or (False, err, None, None, None) on rejection.

        Order of operations matters for security:
          1. Genesis check  cheap, stops wrong-network chains immediately.
          2. Fork point     O(min(local, remote)) hash comparisons.
          3. Build remote_cs: resume from a cached/in-memory state when
             possible, otherwise a full replay from genesis.
          4. is_better_than fork choice, only after we know it's valid.
        """
        if not remote_chain or remote_chain[0]["hash"] != self.cs.genesis_hash:
            log.warning("[sync] rejected genesis mismatch  remote=%s  expected=%s",
                        (remote_chain[0]["hash"][:12] if remote_chain else "empty"),
                        self.cs.genesis_hash[:12])
            return False, "genesis mismatch", None, None, None

        fork_point = next(
            (i for i, (a, b) in enumerate(zip(self.cs.chain, remote_chain))
             if a["hash"] != b["hash"]),
            min(len(self.cs.chain), len(remote_chain))
        )
        tail = remote_chain[fork_point:]

        base_cs = self._resume_point(fork_point, remote_chain)
        if base_cs is not None:
            # Extension or shallow reorg: validate and apply just the new
            # tail onto an already-known state, instead of re-deriving the
            # whole chain from scratch -- ChainState.from_chain (the branch
            # below) replays every block since genesis on every call, so
            # without this a node syncing far behind in many small pages
            # (or hitting a routine reorg) would redo that full replay each
            # time, O(chain length) work per attempt instead of O(new work).
            # Wrapped like the fallback branch below: tail is untrusted peer
            # data, and validation can raise on a malformed block (e.g. one
            # missing a required field) rather than cleanly returning False.
            try:
                cs = base_cs
                for blk in tail:
                    ok, err, cs = cs.validate_and_apply(blk)
                    if not ok:
                        log.warning("[sync] rejected: invalid block at %s: %s",
                                   blk.get("height"), err)
                        return False, f"invalid block at {blk.get('height')}: {err}", None, None, None
                remote_cs = cs
            except Exception as e:
                log.warning("[sync] validation failed: %s", e)
                return False, f"validation error: {e}", None, None, None
        else:
            # Deeper than the cache, or the cache hasn't filled yet (e.g.
            # shortly after a restart): fall back to a full replay. Also
            # the right conservative behavior for what would be a
            # genuinely abnormal, deep divergence. _validate_tail already
            # builds the resulting ChainState as it validates -- reuse it
            # directly rather than replaying the same chain a second time
            # via a separate ChainState.from_chain(remote_chain) call.
            try:
                ok, err, remote_cs = _validate_tail(tail, remote_chain[:fork_point])
            except Exception as e:
                log.warning("[sync] chain replay failed: %s", e)
                return False, f"chain replay error: {e}", None, None, None
            if not ok:
                log.warning("[sync] rejected: %s", err)
                return False, err, None, None, None

        if not remote_cs.is_better_than(self.cs):
            log.debug("[sync] remote chain not better  remote_h=%d  local_h=%d",
                      remote_cs.height, self.cs.height)
            return False, "remote chain not better", fork_point, tail, None

        return True, None, fork_point, tail, remote_cs

    def _readd_valid_txs(self, txs, exclude_hashes, state):
        """Re-add unconfirmed txs to the mempool, each validated against
        state first -- a stale-nonce or otherwise now-invalid tx must not
        be silently re-admitted. Also re-floods each one: it fell out of a
        reorged-away block, so other nodes on the now-canonical chain may
        never have seen it at all, and without this it would just sit in
        our own mempool alone until we mine it ourselves or it expires."""
        for t in txs:
            if tx_mod.tx_hash(t) in exclude_hashes:
                continue
            ok, _ = tx_mod.validate(t, state)
            if ok:
                added, _ = self.mempool.add(t)
                if added:
                    self.gossip.dandelion_send(t, 0)

    def _salvage_fork_txs(self, fork_point, tail):
        """Re-add unconfirmed, still-valid txs from a rejected fork into the
        local mempool. self.cs is unchanged here (the remote chain lost),
        so txs are checked against the current local state."""
        confirmed = {tx_mod.tx_hash(t)
                     for blk in self.cs.chain[fork_point:]
                     for t in blk.get("transactions", [])}
        txs = (t for blk in tail for t in blk.get("transactions", []))
        self._readd_valid_txs(txs, confirmed, self.cs.state)

    def apply_better_chain(self, remote_chain):
        """Accept remote_chain if it is better than local. Used by syncer."""
        ok, err, fork_point, tail, remote_cs = self._evaluate_remote_chain(remote_chain)
        if not ok:
            if fork_point is not None and tail:
                self._salvage_fork_txs(fork_point, tail)
            return False, err

        # Storage write first; if it fails, mempool and self.cs stay untouched
        # and consistent with each other.
        self.storage.replace_chain_and_state(fork_point, tail, remote_cs.state)
        old_chain = self.cs.chain  # abandoned branch; _reorg_mempool needs
                                    # this, not the new chain we're about to swap in
        # self.cs/self.view must track storage the instant it succeeds. A
        # failure past this point (e.g. a malformed re-added tx) must not
        # leave storage on the new chain while self.cs -- what mining and
        # validation actually run against -- still points at the old one.
        # Stale cache entries at or above fork_point belonged to the branch
        # just abandoned; a later reorg landing on one of those heights
        # must not reuse state from blocks that are no longer canonical.
        self._forget_states_from(fork_point)
        self.cs = remote_cs
        self._remember_state(self.cs)
        self.view = NodeView(self.cs)
        try:
            self._reorg_mempool(fork_point, old_chain, remote_chain, remote_cs.state)
        except Exception:
            log.exception("[reorg] mempool re-add failed; chain state already "
                          "committed, mempool may hold stale entries until pruned")

        if fork_point < self.cs.height:
            log.warning("[reorg] height=%d  fork_point=%d", self.cs.height, fork_point)
        else:
            log.info("[sync] height=%d  fork_point=%d", self.cs.height, fork_point)
        return True, None

    def _reorg_mempool(self, fork_point, old_chain, new_chain, new_state):
        """Re-add unconfirmed txs from the abandoned local branch, validated
        against the new chain's state (not the old chain being replaced --
        a tx that's no longer valid under the new chain must not be
        silently re-admitted, or it can stall this node's own block
        production every cycle until it's pruned)."""
        old_txs = [t for blk in old_chain[fork_point:]
                   for t in blk.get("transactions", [])]
        new_confirmed = {tx_mod.tx_hash(t)
                         for blk in new_chain[fork_point:]
                         for t in blk.get("transactions", [])}
        self.mempool.remove_many(new_confirmed)
        self._readd_valid_txs(old_txs, new_confirmed, new_state)
