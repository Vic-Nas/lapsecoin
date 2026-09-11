"""
Unit tests for node.py

Covers every non-VDF method:
  _validate_tail, NodeView, Node._load_cs, Node.is_signing_active,
  Node.mark_tx_seen, Node.get_info, Node.submit_tx, Node.build_and_sign_tx,
  Node._pick_winner, Node._commit, Node._drain_queue, Node._handle,
  Node._handle_inbound_tx, Node._evaluate_remote_chain,
  Node.apply_better_chain, Node._reorg_mempool.

Storage is backed by a real SQLite in-memory equivalent (tmp_path).
Gossip, syncer, pool, net_in_q are mocked. VDF is mocked.
"""

import os
import sys
import queue
import threading
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import block as block_mod
import crypto
import node as node_mod
import state as state_mod
import tx as tx_mod
from chainstate import ChainState
from node import Node, NodeView, _validate_tail
from params import TICKS_PER_LAPSE
from tests.fixtures import (
    address, genesis, keypair, make_block, make_tx,
)


# ---------------------------------------------------------------------------
# VDF mock -- applied everywhere
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_vdf(monkeypatch):
    monkeypatch.setattr("block.vdf_mod.verify", lambda *a, **kw: True)


# ---------------------------------------------------------------------------
# Node factory -- creates a real Node with mocked networking deps
# ---------------------------------------------------------------------------

@pytest.fixture
def node_env(tmp_path):
    """Return (node, keyfile_path) with real storage, mocked net."""
    sk, pk = keypair(0)
    keyfile = str(tmp_path / "node.key")
    passphrase = "testpass"
    crypto.save_key(keyfile, sk, pk, passphrase)
    kek = crypto.derive_kek(keyfile, passphrase)

    gossip  = MagicMock()
    gossip.mark_seen.return_value = False
    syncer  = MagicMock()
    # Real check_and_sync returns True only when it actually adopted a
    # better chain (syncer.py docstring); a bare MagicMock() call would
    # otherwise return a truthy Mock by default and make _run_cycle's
    # mid-wait poll think every tick found a better chain, cancelling the
    # in-flight VDF for no reason. Tests that want to simulate a real
    # mid-wait reorg override this explicitly (see TestRunCycleSyncPolling).
    syncer.check_and_sync.return_value = False
    pool    = MagicMock()
    pool.count.return_value = 3
    net_q   = queue.Queue()
    db_path = str(tmp_path / "chain.db")

    node = Node(
        keyfile=keyfile,
        public_key=pk,
        gossip=gossip,
        syncer=syncer,
        pool=pool,
        net_in_q=net_q,
        db_path=db_path,
    )
    node._loop_thread = threading.current_thread()
    node._kek = kek
    # Real default is 15s (see params.SETTLE_WINDOW_SECONDS_DEFAULT) -- a
    # deliberate post-finish wait for straggler candidates, not a bug, but
    # it would make every _run_cycle() test call take 15+ real wall-clock
    # seconds. Tests that specifically exercise the settle window override
    # this themselves.
    node.settle_window_seconds = 0.01
    return node, keyfile, kek, gossip, syncer, pool, net_q


def fresh_state():
    return state_mod.State()


# ---------------------------------------------------------------------------
# 1. _validate_tail (module-level pure function)
# ---------------------------------------------------------------------------

class TestValidateTail:
    def test_empty_tail_passes(self):
        ok, err, cs = _validate_tail([], [genesis()])
        assert ok is True, err
        assert cs.height == 0

    def test_single_valid_block_passes(self):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        ok, err, cs = _validate_tail([b1], [g])
        assert ok is True, err
        assert cs.height == 1

    def test_invalid_block_in_tail_fails(self):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        b1["previous_hash"] = "00" * 32
        b1["hash"] = block_mod.block_hash(b1)
        ok, err, cs = _validate_tail([b1], [g])
        assert ok is False
        assert cs is None

    def test_two_valid_blocks_passes(self):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        b2 = make_block(2, b1["hash"], [])
        ok, err, cs = _validate_tail([b1, b2], [g])
        assert ok is True, err
        assert cs.height == 2


# ---------------------------------------------------------------------------
# 2. NodeView
# ---------------------------------------------------------------------------

class TestNodeView:
    def test_node_view_exposes_chain_properties(self):
        cs = ChainState.from_genesis()
        v = NodeView(cs)
        assert v.height == 0
        assert v.tip["hash"] == cs.tip["hash"]
        assert v.genesis_hash == cs.genesis_hash

    def test_node_view_state_is_snapshot(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 1000)
        v = NodeView(cs)
        # Mutate original -- view's snapshot should not change
        cs.state.credit(address(0), 9999)
        assert v.state.get_balance(address(0)) == 1000


# ---------------------------------------------------------------------------
# 3. Node._load_cs (genesis path and reload path)
# ---------------------------------------------------------------------------

class TestLoadCs:
    def test_load_cs_creates_genesis_when_empty(self, node_env):
        node, *_ = node_env
        assert node.cs.height == 0

    def test_load_cs_genesis_saved_to_storage(self, node_env):
        node, *_ = node_env
        stored = node.storage.load_all_blocks()
        assert len(stored) == 1
        assert stored[0]["height"] == 0

    def test_load_cs_reloads_from_storage(self, tmp_path):
        """Second Node creation with same db_path reloads existing chain."""
        sk, pk = keypair(1)
        keyfile = str(tmp_path / "node2.key")
        crypto.save_key(keyfile, sk, pk, "pass")
        db_path = str(tmp_path / "chain2.db")

        # First node -- creates genesis
        n1 = Node(
            keyfile=keyfile, public_key=pk,
            gossip=MagicMock(), syncer=MagicMock(),
            pool=MagicMock(), net_in_q=queue.Queue(),
            db_path=db_path,
        )
        assert n1.cs.height == 0

        # Second node -- should reload genesis from db
        n2 = Node(
            keyfile=keyfile, public_key=pk,
            gossip=MagicMock(), syncer=MagicMock(),
            pool=MagicMock(), net_in_q=queue.Queue(),
            db_path=db_path,
        )
        assert n2.cs.height == 0
        assert n2.cs.tip["hash"] == n1.cs.tip["hash"]


# ---------------------------------------------------------------------------
# 4. Simple accessors
# ---------------------------------------------------------------------------

class TestSimpleAccessors:
    def test_is_signing_active_true_when_kek_set(self, node_env):
        node, *_ = node_env
        assert node.is_signing_active() is True

    def test_is_signing_active_false_when_no_kek(self, node_env):
        node, *_ = node_env
        node._kek = None
        assert node.is_signing_active() is False

    def test_mark_tx_seen_delegates_to_gossip(self, node_env):
        node, _, __, gossip, *_ = node_env
        gossip.mark_seen.return_value = False
        result = node.mark_tx_seen("abc")
        gossip.mark_seen.assert_called_once_with("abc")
        assert result is False

    def test_get_info_returns_expected_keys(self, node_env):
        node, *_ = node_env
        info = node.get_info()
        for key in ["height", "tip_hash", "genesis_hash",
                    "mempool_size", "address", "peer_count", "total_minted",
                    "can_mint", "block_reward"]:
            assert key in info

    def test_get_info_can_mint_is_the_pool_not_the_reward(self, node_env):
        """can_mint is the mintable pool; block_reward is one block's cut of it.

        These were once the same key holding two different values: /api/info
        returned the per-block reward under the name can_mint while
        /api/stats returned the pool under that same name.
        """
        node, *_ = node_env
        info = node.get_info()
        sv   = node.view.state
        assert info["can_mint"]     == sv.compute_can_mint()
        assert info["block_reward"] == sv.compute_block_reward()
        assert info["block_reward"] < info["can_mint"]

    def test_get_info_height_is_zero_at_genesis(self, node_env):
        node, *_ = node_env
        assert node.get_info()["height"] == 0

    def test_block_time_ratio_is_none_before_any_build(self, node_env):
        node, *_ = node_env
        assert node.get_info()["block_time_ratio"] is None

    def test_block_time_ratio_is_none_with_only_genesis_even_if_own_builds_exist(self, node_env):
        """No chain-side median is possible with just genesis (no block-to-
        block delta exists yet), so the comparison has nothing to compare
        against even though this node has real build history."""
        node, *_ = node_env
        node._own_build_seconds.append(130.0)
        assert node.get_info()["block_time_ratio"] is None

    def test_block_time_ratio_compares_own_median_to_chain_median(self, node_env):
        """block_time_ratio must be this node's own median build time divided
        by the chain's own recent median block-to-block time -- not this
        node's latest build vs its own history, and not tied to whichever
        node happened to build the current tip."""
        node, *_ = node_env
        cs = ChainState.from_genesis()
        for h in range(1, 6):
            # 150s real gap at every height (120 base + 30*h offset).
            blk = make_block(h, cs.tip["hash"], [], timestamp_offset=30 * h)
            cs = cs.apply_block(blk)
        node.cs = cs
        node.view = NodeView(cs)
        for seconds in [130.0] * 10:
            node._own_build_seconds.append(seconds)
        assert node.get_info()["block_time_ratio"] == pytest.approx(130.0 / 150.0)

    def test_block_time_ratio_window_caps_at_30(self, node_env):
        node, *_ = node_env
        for seconds in [100.0] * 40:
            node._own_build_seconds.append(seconds)
        assert len(node._own_build_seconds) == 30


# ---------------------------------------------------------------------------
# 5. submit_tx
# ---------------------------------------------------------------------------

class TestSubmitTx:
    def test_submit_valid_tx_returns_true(self, node_env):
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        ok, result = node.submit_tx(t)
        assert ok is True
        assert len(result) == 64

    def test_submit_tx_adds_to_mempool(self, node_env):
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        node.submit_tx(t)
        assert node.mempool.size() == 1

    def test_submit_tx_enters_propagation(self, node_env):
        node, _, __, gossip, *_ = node_env
        node.cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        node.submit_tx(t)
        gossip.spread.assert_called_once()

    def test_submit_invalid_tx_returns_false(self, node_env):
        node, *_ = node_env
        # No balance for address(5)
        t = make_tx(5, 1, TICKS_PER_LAPSE, node.cs.state)
        ok, err = node.submit_tx(t)
        assert ok is False

    def test_submit_duplicate_tx_returns_false(self, node_env):
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        node.submit_tx(t)
        ok, err = node.submit_tx(t)
        assert ok is False


# ---------------------------------------------------------------------------
# 6. build_and_sign_tx
# ---------------------------------------------------------------------------

class TestBuildAndSignTx:
    def test_build_and_sign_returns_tx_and_fee(self, node_env):
        node, keyfile, *_ = node_env
        node.cs.state.credit(node.addr, 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE
        # Rebuild view so it reflects the updated state
        from node import NodeView
        node.view = NodeView(node.cs)
        outputs = [{"to": address(1), "amount": TICKS_PER_LAPSE}]
        t, fee = node.build_and_sign_tx(outputs, fee=100, passphrase="testpass")
        assert isinstance(t, dict)
        assert "signature" in t
        assert fee == 100

    def test_build_and_sign_signature_verifies(self, node_env):
        node, keyfile, *_ = node_env
        node.cs.state.credit(node.addr, 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE
        from node import NodeView
        node.view = NodeView(node.cs)
        outputs = [{"to": address(1), "amount": TICKS_PER_LAPSE}]
        t, _ = node.build_and_sign_tx(outputs, passphrase="testpass")
        ok, err = tx_mod.validate(t, node.cs.state)
        assert ok is True, err


# ---------------------------------------------------------------------------
# 7. _pick_winner
# ---------------------------------------------------------------------------

class TestPickWinner:
    def _make_candidate(self, cs):
        g = cs.tip
        blk = make_block(g["height"] + 1, g["hash"], [], builder_index=0)
        return blk

    def test_own_candidate_wins_when_no_peers(self, node_env):
        node, *_ = node_env
        candidate = self._make_candidate(node.cs)
        winner, relay = node._pick_winner(node.cs, candidate, [])
        assert winner is candidate
        assert relay is False

    def test_stale_candidate_returns_none(self, node_env):
        node, *_ = node_env
        candidate = make_block(1, "00" * 32, [])  # wrong previous_hash
        winner, relay = node._pick_winner(node.cs, candidate, [])
        assert winner is None

    def test_invalid_peer_block_ignored(self, node_env):
        node, *_ = node_env
        candidate = self._make_candidate(node.cs)
        # Peer block with wrong height
        bad_peer = make_block(99, node.cs.tip["hash"], [])
        winner, relay = node._pick_winner(node.cs, candidate, [bad_peer])
        assert winner is candidate

    def test_lowest_vdf_output_peer_block_wins(self, node_env):
        node, *_ = node_env
        g = node.cs.tip
        peer_blk  = make_block(1, g["hash"], [], builder_index=1, vdf_output="aa")
        candidate = make_block(1, g["hash"], [], builder_index=0, vdf_output="bb")
        winner, relay = node._pick_winner(node.cs, candidate, [peer_blk])
        # Same rule as ChainState.is_better_than: lowest vdf_output wins,
        # not whichever arrived first.
        assert winner is peer_blk
        assert relay is True

    def test_own_candidate_wins_tie_break_over_peer(self, node_env):
        node, *_ = node_env
        g = node.cs.tip
        peer_blk  = make_block(1, g["hash"], [], builder_index=1, vdf_output="zz")
        candidate = make_block(1, g["hash"], [], builder_index=0, vdf_output="aa")
        winner, relay = node._pick_winner(node.cs, candidate, [peer_blk])
        assert winner is candidate
        assert relay is False


# ---------------------------------------------------------------------------
# 8. _commit
# ---------------------------------------------------------------------------

class TestCommit:
    def test_commit_updates_chainstate_height(self, node_env):
        node, *_ = node_env
        blk = make_block(1, node.cs.tip["hash"], [])
        node._commit(blk)
        assert node.cs.height == 1

    def test_commit_removes_confirmed_txs_from_mempool(self, node_env):
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        node.mempool.add(t)
        blk = make_block(1, node.cs.tip["hash"], [t])
        node._commit(blk)
        assert node.mempool.size() == 0

    def test_commit_updates_view(self, node_env):
        node, *_ = node_env
        old_view = node.view
        blk = make_block(1, node.cs.tip["hash"], [])
        node._commit(blk)
        assert node.view is not old_view
        assert node.view.height == 1

    def test_commit_does_not_re_propagate(self, node_env):
        """Propagation happens once, where the block first appears: a peer's
        when it arrives (_handle_inbound_block), our own when we build it.
        Re-sending at commit would just be a second copy of something the
        network already has."""
        node, _, __, gossip, *_ = node_env
        g = node.cs.tip
        blk = make_block(1, g["hash"], [])
        node._commit(blk, relay=True)
        gossip.spread.assert_not_called()
        gossip.relay.assert_not_called()

# ---------------------------------------------------------------------------
# 9. _drain_queue / _handle
# ---------------------------------------------------------------------------

class TestDrainQueue:
    def test_drain_empty_queue_returns_empty(self, node_env):
        node, _, __, ___, ____, _____, net_q = node_env
        blocks = node._drain_queue()
        assert blocks == []

    def test_drain_block_message(self, node_env):
        node, _, __, ___, ____, _____, net_q = node_env
        blk = make_block(1, node.cs.tip["hash"], [])
        net_q.put({"type": "block", "block": blk})
        blocks = node._drain_queue()
        assert len(blocks) == 1
        assert blocks[0]["hash"] == blk["hash"]

    def test_drain_submit_tx_message(self, node_env):
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        reply = queue.Queue()
        node.net_in_q.put({"type": "submit_tx", "tx": t, "reply": reply})
        node._drain_queue()
        ok, _ = reply.get_nowait()
        assert ok is True

    def test_drain_unknown_message_type_ignored(self, node_env):
        node, _, __, ___, ____, _____, net_q = node_env
        net_q.put({"type": "unknown_garbage"})
        blocks = node._drain_queue()  # must not raise
        assert blocks == []


# ---------------------------------------------------------------------------
# 10. _handle_inbound_tx
# ---------------------------------------------------------------------------

class TestHandleInboundTx:
    def test_fluff_valid_tx_added_to_mempool(self, node_env):
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        msg = {"tx": t, "sender": "1.2.3.4:1", "stemming": False}
        node._handle_inbound_tx(msg)
        assert node.mempool.size() == 1

    def test_fluff_invalid_tx_not_added(self, node_env):
        node, *_ = node_env
        # No balance for address(5)
        t = make_tx(5, 1, TICKS_PER_LAPSE, node.cs.state)
        msg = {"tx": t, "sender": "1.2.3.4:1", "stemming": False}
        node._handle_inbound_tx(msg)
        assert node.mempool.size() == 0

    def test_fluff_duplicate_not_added_again(self, node_env):
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        msg = {"tx": t, "sender": "1.2.3.4:1", "stemming": False}
        node._handle_inbound_tx(msg)
        node._handle_inbound_tx(msg)  # second time -- duplicate
        assert node.mempool.size() == 1

    def test_stem_tx_is_validated_before_being_forwarded(self, node_env):
        """A tx still in the private phase is forwarded rather than admitted
        -- we're a relay for it, not its destination -- but it is validated
        first. Relaying something unvalidated would let anyone spend our
        bandwidth, and every downstream peer's, for one crafted datagram."""
        node, _, __, gossip, *_ = node_env
        node.cs.state.credit(address(0), 10 * TICKS_PER_LAPSE)
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        node._handle_inbound_tx({"tx": t, "sender": "1.2.3.4:1", "stemming": True})
        gossip.relay.assert_called_once()
        assert gossip.relay.call_args.args[3] == "1.2.3.4:1"
        assert gossip.relay.call_args.kwargs["stemming"] is True
        # forwarded, not admitted
        assert node.mempool.size() == 0

    def test_invalid_stem_tx_is_not_forwarded(self, node_env):
        node, _, __, gossip, *_ = node_env
        t = make_tx(0, 1, TICKS_PER_LAPSE, fresh_state())  # sender has no balance
        node._handle_inbound_tx({"tx": t, "sender": "1.2.3.4:1", "stemming": True})
        gossip.relay.assert_not_called()

    def test_public_tx_is_admitted_and_passed_on(self, node_env):
        node, _, __, gossip, *_ = node_env
        node.cs.state.credit(address(0), 10 * TICKS_PER_LAPSE)
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        node._handle_inbound_tx({"tx": t, "sender": "1.2.3.4:1", "stemming": False})
        assert node.mempool.size() == 1
        gossip.relay.assert_called_once()
        assert gossip.relay.call_args.kwargs["stemming"] is False


# ---------------------------------------------------------------------------
# 11. _evaluate_remote_chain
# ---------------------------------------------------------------------------

class TestEvaluateRemoteChain:
    def test_empty_remote_chain_fails(self, node_env):
        node, *_ = node_env
        ok, err, *_ = node._evaluate_remote_chain([])
        assert ok is False

    def test_wrong_genesis_fails(self, node_env):
        node, *_ = node_env
        wrong_g = dict(genesis())
        wrong_g["message"] = "tampered"
        wrong_g["hash"] = block_mod.block_hash(wrong_g)
        ok, err, *_ = node._evaluate_remote_chain([wrong_g])
        assert ok is False
        assert "genesis" in err

    def test_not_better_than_local_fails(self, node_env):
        node, *_ = node_env
        # Remote chain is identical (same genesis only)
        remote = [node.cs.chain[0]]
        ok, err, *_ = node._evaluate_remote_chain(remote)
        assert ok is False
        assert "not better" in err

    def test_longer_valid_remote_chain_accepted(self, node_env):
        node, *_ = node_env
        g = node.cs.chain[0]
        b1 = make_block(1, g["hash"], [])
        b2 = make_block(2, b1["hash"], [])
        ok, err, fork_point, tail, remote_cs = node._evaluate_remote_chain([g, b1, b2])
        assert ok is True, err
        assert remote_cs.height == 2

    def test_invalid_tail_block_fails(self, node_env):
        node, *_ = node_env
        g = node.cs.chain[0]
        bad_b1 = make_block(1, "00" * 32, [])  # wrong previous_hash
        ok, err, *_ = node._evaluate_remote_chain([g, bad_b1])
        assert ok is False

    def test_fork_point_correct_for_same_genesis(self, node_env):
        node, *_ = node_env
        g = node.cs.chain[0]
        b1 = make_block(1, g["hash"], [])
        b2 = make_block(2, b1["hash"], [])
        ok, _, fork_point, tail, _ = node._evaluate_remote_chain([g, b1, b2])
        assert ok is True
        assert fork_point == 1  # local is at height 0, diverge at index 1
        assert len(tail) == 2

    def test_malformed_block_in_extension_rejected_not_raised(self, node_env):
        """A block missing a required field can still have a self-consistent
        hash (block_hash just hashes whatever's present), so validation can
        reach a raw dict access like blk["height"] and raise, instead of
        cleanly returning False. The pure-extension fast path must catch
        that the same way the reorg fallback path already does, rather
        than letting it escape as an unhandled exception."""
        node, *_ = node_env
        g = node.cs.chain[0]
        b1 = make_block(1, g["hash"], [])
        del b1["height"]
        b1["hash"] = block_mod.block_hash(b1)  # stays self-consistent
        ok, err, *_ = node._evaluate_remote_chain([g, b1])
        assert ok is False
        assert err  # some rejection reason, not an unhandled exception


class TestRecentStateCache:
    """_resume_point / _remember_state / _forget_states_from: a shallow
    reorg should resume from a cached state instead of a full replay, and
    that cache must never let a later reorg reuse state from a branch
    that's already been abandoned."""

    def test_shallow_reorg_resumes_from_cache_not_full_replay(self, node_env):
        node, *_ = node_env
        g = node.cs.chain[0]
        b1 = make_block(1, g["hash"], [])
        b2 = make_block(2, b1["hash"], [])
        b3 = make_block(3, b2["hash"], [])
        for b in (b1, b2, b3):
            node._commit(b)
        assert node.cs.height == 3
        assert set(node._recent_states) == {1, 2, 3}

        # A sibling fork at height 3 (same parent b2, different builder) --
        # fork_point=3, resume_height=2, which is cached.
        b3_alt = make_block(3, b2["hash"], [], builder_index=1, vdf_output="00" * 100)

        import unittest.mock as _mock
        with _mock.patch.object(ChainState, "from_chain") as mocked_replay:
            ok, err, fork_point, tail, remote_cs = node._evaluate_remote_chain(
                [g, b1, b2, b3_alt])
        assert ok is True, err
        assert fork_point == 3
        mocked_replay.assert_not_called()
        assert remote_cs.height == 3

    def test_reorg_beyond_cache_falls_back_to_full_replay(self, node_env, monkeypatch):
        node, *_ = node_env
        monkeypatch.setattr(node_mod, "RECENT_STATE_CACHE_SIZE", 1)
        g = node.cs.chain[0]
        b1 = make_block(1, g["hash"], [])
        b2 = make_block(2, b1["hash"], [])
        b3 = make_block(3, b2["hash"], [])
        for b in (b1, b2, b3):
            node._commit(b)
        # Cache holds only the most recent 1 entry now -- height 1 and 2
        # (needed below, as resume_height=1) were evicted, and it's not
        # the free trivial genesis case either (resume_height != 0).
        assert set(node._recent_states) == {3}

        b2_alt = make_block(2, b1["hash"], [], builder_index=1, vdf_output="00" * 100)
        b3_alt = make_block(3, b2_alt["hash"], [], builder_index=1, vdf_output="00" * 100)

        import unittest.mock as _mock
        with _mock.patch.object(ChainState, "from_chain", wraps=ChainState.from_chain) as spy:
            ok, err, *_ = node._evaluate_remote_chain([g, b1, b2_alt, b3_alt])
        assert ok is True, err
        spy.assert_called_once()

    def test_stale_cache_entry_not_reused_after_reorg(self, node_env):
        """Reorg away from b2 (builder 0) to b2_b (builder 1), then reorg
        again to a third sibling b2_c at the same height. The second reorg
        must not reuse b2_b's now-abandoned state under b2's old cache
        slot -- it should resume from the shared, untouched ancestor
        (height 1) instead, same as the first reorg did."""
        node, *_ = node_env
        g = node.cs.chain[0]
        b1 = make_block(1, g["hash"], [])
        b2_a = make_block(2, b1["hash"], [], builder_index=0)
        node._commit(b1)
        node._commit(b2_a)
        assert node.cs.tip["builder"] == address(0)

        b2_b = make_block(2, b1["hash"], [], builder_index=1, vdf_output="50" * 100)
        ok, err = node.apply_better_chain([g, b1, b2_b])
        assert ok is True, err
        assert node.cs.tip["builder"] == address(1)
        # The abandoned b2_a's height was purged, not left stale.
        assert node._recent_states[2][0] is not None
        assert node.cs.state.get_balance(address(1)) > 0

        b2_c = make_block(2, b1["hash"], [], builder_index=2, vdf_output="00" * 100)
        ok, err = node.apply_better_chain([g, b1, b2_c])
        assert ok is True, err
        assert node.cs.tip["builder"] == address(2)
        # Builder 1's reward from the now-abandoned b2_b must not linger.
        assert node.cs.state.get_balance(address(1)) == 0
        assert node.cs.state.get_balance(address(2)) > 0

# ---------------------------------------------------------------------------
# 12. apply_better_chain
# ---------------------------------------------------------------------------

class TestApplyBetterChain:
    def test_apply_better_chain_updates_cs(self, node_env):
        node, *_ = node_env
        g = node.cs.chain[0]
        b1 = make_block(1, g["hash"], [])
        b2 = make_block(2, b1["hash"], [])
        ok, err = node.apply_better_chain([g, b1, b2])
        assert ok is True, err
        assert node.cs.height == 2

    def test_apply_better_chain_updates_view(self, node_env):
        node, *_ = node_env
        g = node.cs.chain[0]
        b1 = make_block(1, g["hash"], [])
        node.apply_better_chain([g, b1])
        assert node.view.height == 1

    def test_apply_worse_chain_rejected(self, node_env):
        node, *_ = node_env
        g = node.cs.chain[0]
        # Same-height, same chain -- not better
        ok, err = node.apply_better_chain([g])
        assert ok is False

    def test_apply_better_chain_wrong_genesis_rejected(self, node_env):
        node, *_ = node_env
        wrong_g = dict(genesis())
        wrong_g["message"] = "tampered"
        wrong_g["hash"] = block_mod.block_hash(wrong_g)
        ok, err = node.apply_better_chain([wrong_g])
        assert ok is False
        assert "genesis" in err


# ---------------------------------------------------------------------------
# 13. _reorg_mempool
# ---------------------------------------------------------------------------

class TestReorgMempool:
    def test_reorg_restores_old_chain_txs(self, node_env):
        """Txs from the old chain that aren't in the new chain go back to mempool."""
        node, *_ = node_env
        g = node.cs.chain[0]
        # b0 is shared by both branches, so address(0)'s balance comes from
        # a real block reward replayed in the common prefix -- no need to
        # hack a balance into a mocked from_chain, which also means the
        # reorg here lands within _resume_point's cache (fork_point=2,
        # resume_height=1, populated by the _commit(b0) below), exercising
        # the actual fast path rather than a full replay.
        b0 = make_block(1, g["hash"], [], builder_index=0)
        node._commit(b0)
        assert node.cs.height == 1

        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        b1_old = make_block(2, b0["hash"], [t])
        node._commit(b1_old)
        assert node.cs.height == 2

        # New chain shares b0 but replaces b1 with one that doesn't include t.
        b1_new = make_block(2, b0["hash"], [], builder_index=1)
        b2_new = make_block(3, b1_new["hash"], [], builder_index=1)
        full_chain = [g, b0, b1_new, b2_new]

        import unittest.mock as _mock
        with _mock.patch.object(node.cs.__class__, "is_better_than", return_value=True):
            ok, err = node.apply_better_chain(full_chain)
        assert ok is True, err
        # t was in the old chain at fork_point=2, is not in the new chain,
        # and is still valid (nonce/balance) against the new chain's state.
        assert node.mempool.get(tx_mod.tx_hash(t)) is not None

    def test_reorg_drops_confirmed_txs(self, node_env):
        """Txs confirmed in both old and new chains are NOT restored to the mempool.
        Calls _reorg_mempool directly since we're testing its logic, not full validation.
        """
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE

        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        h = tx_mod.tx_hash(t)
        g = node.cs.chain[0]

        # Place t in the old chain (node.cs.chain[1]) so _reorg_mempool sees it
        b1_old = make_block(1, g["hash"], [t])
        # Manually set cs to a chain containing b1_old so old_txs picks up t
        node.cs = ChainState.from_genesis()
        node.cs.chain.append(b1_old)  # add to chain list directly

        # New chain also contains t (same tx confirmed there too)
        b1_new = make_block(1, g["hash"], [t], builder_index=1)
        node._reorg_mempool(fork_point=1, old_chain=node.cs.chain,
                           new_chain=[g, b1_new], new_state=node.cs.state)

        # t is confirmed in new chain -> must NOT appear in mempool
        assert node.mempool.get(h) is None

    def test_reorg_does_not_readd_tx_invalid_under_new_state(self, node_env):
        """A tx from the abandoned branch that's no longer valid against the
        new chain's state (e.g. its nonce is already used there by a
        different tx) must not be silently re-admitted -- doing so would
        make every subsequent self-produced block fail validation."""
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        node.cs.state.total_minted += 100 * TICKS_PER_LAPSE

        g = node.cs.chain[0]
        t_old = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)  # nonce 1
        b1_old = make_block(1, g["hash"], [t_old])
        node.cs = ChainState.from_genesis()
        node.cs.chain.append(b1_old)

        # New chain confirms a *different* tx from address(0) at the same
        # nonce, so t_old's nonce is now stale against the new state.
        new_state = state_mod.State()
        new_state.credit(address(0), 100 * TICKS_PER_LAPSE)
        new_state.total_minted += 100 * TICKS_PER_LAPSE
        t_new = make_tx(0, 2, TICKS_PER_LAPSE, new_state, nonce_override=1)
        new_state.apply_tx(t_new)
        b1_new = make_block(1, g["hash"], [t_new], builder_index=1)

        node._reorg_mempool(fork_point=1, old_chain=node.cs.chain,
                           new_chain=[g, b1_new], new_state=new_state)

        assert node.mempool.get(tx_mod.tx_hash(t_old)) is None


# ---------------------------------------------------------------------------
# 15. _run_cycle: event-driven sync and the staleness guard
# ---------------------------------------------------------------------------

class TestRunCycleSync:
    """Sync happens on evidence, not on a schedule. The evidence is an
    inbound block above our tip, which propagation delivers for free and
    which names the peer holding it. If a sync adopts a better chain
    mid-wait, the in-flight VDF was computed for a tip that no longer
    exists and must be discarded, not committed."""

    def _slow_fake_evaluate(self, sleep_seconds):
        def _evaluate(challenge, iterations, handle=None):
            time.sleep(sleep_seconds)
            return "aa" * 100, "bb" * 100, sleep_seconds
        return _evaluate

    def test_quiet_network_does_not_poll_at_all(self, node_env, monkeypatch):
        """Nothing arrived and the chain is keeping pace, so there is
        nothing to ask anyone about. The old code paid a round trip every
        cycle plus one every 10s regardless."""
        node, *_, syncer, pool, net_q = node_env
        monkeypatch.setattr(node_mod.vdf_mod, "evaluate", self._slow_fake_evaluate(0.05))

        node._run_cycle()

        syncer.check_and_sync.assert_not_called()

    def test_block_above_our_tip_triggers_a_sync_against_its_sender(
        self, node_env, monkeypatch
    ):
        node, *_, syncer, pool, net_q = node_env
        monkeypatch.setattr(node_mod.vdf_mod, "evaluate", self._slow_fake_evaluate(0.05))
        ahead = make_block(7, "00" * 32, [])
        net_q.put({"type": "block", "block": ahead, "sender": "9.9.9.9:1"})

        node._run_cycle()

        syncer.check_and_sync.assert_called_once()
        assert syncer.check_and_sync.call_args.kwargs["peer"] == "9.9.9.9:1"

    def test_a_block_at_or_below_our_tip_triggers_nothing(self, node_env, monkeypatch):
        node, *_, syncer, pool, net_q = node_env
        monkeypatch.setattr(node_mod.vdf_mod, "evaluate", self._slow_fake_evaluate(0.05))
        g = node.cs.tip
        net_q.put({"type": "block", "block": g, "sender": "9.9.9.9:1"})

        node._run_cycle()

        syncer.check_and_sync.assert_not_called()

    def test_silence_past_the_chains_own_pace_polls_the_highest_peer(
        self, node_env, monkeypatch
    ):
        """The one case no inbound block can ever report: nothing is
        arriving at all. Threshold comes from the chain's measured median
        interval, and the peer from heights the pool already caches."""
        node, *_, syncer, pool, net_q = node_env
        monkeypatch.setattr(node_mod.vdf_mod, "evaluate", self._slow_fake_evaluate(0.05))
        # Threshold longer than this test's cycle, so the one poll silence
        # earns isn't repeated on every loop tick.
        monkeypatch.setattr(node, "_silence_threshold", lambda: 5.0)
        node._last_block_seen = time.monotonic() - 60
        pool.snapshot.return_value = [
            ("low:1", 0, True, 3, "", "", "", None),
            ("high:1", 0, True, 99, "", "", "", None),
            ("offline:1", 0, False, 500, "", "", "", None),
        ]

        node._run_cycle()

        syncer.check_and_sync.assert_called_once()
        assert syncer.check_and_sync.call_args.kwargs["peer"] == "high:1"

    def test_mid_wait_reorg_discards_stale_candidate(self, node_env, monkeypatch):
        """Evidence that arrives *after* the VDF has started. The proof we
        end up with was computed for a tip that no longer exists, so it has
        to be thrown away rather than spliced onto the wrong parent.

        (Evidence arriving *before* the VDF starts is a different and better
        case: the cycle simply builds on the corrected tip, no work wasted.
        That's what test_block_above_our_tip_triggers_a_sync covers.)"""
        node, *_, syncer, pool, net_q = node_env
        replacement_cs = ChainState.from_genesis()

        def fake_check_and_sync(chain, apply_fn, **kwargs):
            node.cs = replacement_cs
            return True

        def evaluate_then_land_a_reorg(challenge, iterations, handle=None):
            net_q.put({"type": "block",
                       "block": make_block(7, "00" * 32, []),
                       "sender": "9.9.9.9:1"})
            time.sleep(0.3)
            return "aa" * 100, "bb" * 100, 0.3

        monkeypatch.setattr(node_mod.vdf_mod, "evaluate", evaluate_then_land_a_reorg)
        syncer.check_and_sync.side_effect = fake_check_and_sync
        commit_spy = MagicMock()
        monkeypatch.setattr(node, "_commit", commit_spy)

        node._run_cycle()

        assert node.cs is replacement_cs
        commit_spy.assert_not_called()

    def test_no_stale_reorg_commits_normally(self, node_env, monkeypatch):
        node, *_ = node_env
        monkeypatch.setattr(node_mod.vdf_mod, "evaluate", self._slow_fake_evaluate(0.15))
        commit_spy = MagicMock()
        monkeypatch.setattr(node, "_commit", commit_spy)

        node._run_cycle()

        commit_spy.assert_called_once()

    def test_status_line_reflects_vdf_computation(self, node_env, monkeypatch):
        node, *_ = node_env
        monkeypatch.setattr(node_mod.vdf_mod, "evaluate", self._slow_fake_evaluate(0.05))

        node._run_cycle()

        assert "block 1" in node.status_line

    def test_status_line_updates_on_heartbeat(self, node_env, monkeypatch):
        node, *_ = node_env
        monkeypatch.setattr(node_mod, "VDF_HEARTBEAT_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(node_mod.vdf_mod, "evaluate", self._slow_fake_evaluate(0.3))

        seen = []
        real_drain = node._drain_queue

        def spying_drain(*args, **kwargs):
            seen.append(node.status_line)
            return real_drain(*args, **kwargs)

        monkeypatch.setattr(node, "_drain_queue", spying_drain)

        node._run_cycle()

        assert any("elapsed" in s for s in seen)


# ---------------------------------------------------------------------------
# 16. _run_cycle: settle window (abandon own VDF once it can't matter)
# ---------------------------------------------------------------------------

class TestRunCycleSettleWindow:
    """A peer's validated candidate for the height we're building arms a
    short settle timer; once it elapses without our own VDF finishing, we
    abort our own attempt and resolve the height from whatever candidates
    we have instead of grinding to completion against a height that's
    likely already settled elsewhere. Nothing unvalidated may ever reach
    this path (see _validate_candidate's docstring)."""

    def _cancellable_fake_evaluate(self, total_seconds, poll=0.02):
        """Unlike _slow_fake_evaluate, this one actually honors handle,
        the same way real chiavdf's shutdown_file polling does -- needed
        here since these tests assert on the cancel path actually firing
        promptly, not just on it eventually returning garbage."""
        def _evaluate(challenge, iterations, handle=None):
            elapsed = 0.0
            while elapsed < total_seconds:
                if handle is not None and handle._cancelled:
                    raise node_mod.vdf_mod.Cancelled()
                time.sleep(poll)
                elapsed += poll
            return "aa" * 100, "bb" * 100, total_seconds
        return _evaluate

    def test_peer_candidate_arms_settle_window(
        self, node_env, monkeypatch
    ):
        node, *_, gossip, syncer, pool, net_q = node_env
        node.settle_window_seconds = 0.05
        monkeypatch.setattr(node_mod.vdf_mod, "evaluate",
                            self._cancellable_fake_evaluate(2.0))
        commit_spy = MagicMock(wraps=node._commit)
        monkeypatch.setattr(node, "_commit", commit_spy)

        g = node.cs.tip
        peer_blk = make_block(1, g["hash"], [], builder_index=1, vdf_output="aa")
        net_q.put({"type": "block", "block": peer_blk})

        node._run_cycle()

        # Own attempt abandoned; the peer's block is the only entrant, so
        # it's what gets committed.
        commit_spy.assert_called_once()
        winner = commit_spy.call_args.args[0]
        assert winner["hash"] == peer_blk["hash"]
        assert commit_spy.call_args.kwargs.get("relay") is True

    def test_own_vdf_finishing_first_still_commits_normally(
        self, node_env, monkeypatch
    ):
        """Sanity check: with no peer candidate ever arriving, a fast own
        VDF still wins the way it always did -- the settle window just
        adds its own short (here: tiny) wait afterward before finalizing."""
        node, *_ = node_env
        node.settle_window_seconds = 0.01
        monkeypatch.setattr(node_mod.vdf_mod, "evaluate",
                            self._cancellable_fake_evaluate(0.05))
        commit_spy = MagicMock()
        monkeypatch.setattr(node, "_commit", commit_spy)

        node._run_cycle()

        commit_spy.assert_called_once()

    def test_unvalidated_block_never_arms_settle_window_or_gets_relayed(
        self, node_env, monkeypatch
    ):
        """A garbage/invalid same-height message must never trigger relay
        or the settle timer -- see _validate_candidate's
        docstring on why (a free griefing vector otherwise: crafting a
        fake block costs nothing, but aborting a real in-flight VDF does
        not). Own VDF should finish and win normally, undisturbed."""
        node, *_, gossip, syncer, pool, net_q = node_env
        node.settle_window_seconds = 0.05
        monkeypatch.setattr(node_mod.vdf_mod, "evaluate",
                            self._cancellable_fake_evaluate(0.15))
        commit_spy = MagicMock(wraps=node._commit)
        monkeypatch.setattr(node, "_commit", commit_spy)

        g = node.cs.tip
        garbage = make_block(1, g["hash"], [], builder_index=1, vdf_output="aa")
        # An out-of-range height fails _validate_candidate's
        # height check unconditionally, regardless of what the autouse
        # vdf.verify mock would otherwise let through.
        garbage["height"] = 999
        net_q.put({"type": "block", "block": garbage})

        node._run_cycle()

        commit_spy.assert_called_once()
        winner = commit_spy.call_args.args[0]
        assert winner.get("builder") == node.addr  # own candidate won, not the garbage


# ---------------------------------------------------------------------------
# 17. Rework: an item that never comes back gets re-sent
# ---------------------------------------------------------------------------

class TestRework:
    """A stem hands an item to one peer and forgets it. If that peer is a
    dead end (its only link is back to us) or the datagram is lost, the item
    stops there and nobody else hears about it -- and the sender cannot tell
    either case from success. Noticing it never came back is the only signal
    available, and it's what makes stemming safe on a graph we can't see.
    Without it, a block could cost its builder the whole evaluation."""

    def test_item_that_echoes_back_is_not_re_sent(self, node_env):
        node, _, __, gossip, *_ = node_env
        node.cs.state.credit(address(0), 10 * TICKS_PER_LAPSE)
        t = make_tx(0, 1, TICKS_PER_LAPSE, node.cs.state)
        h = tx_mod.tx_hash(t)

        node._spread(t, "tx", h)
        assert h in node._unconfirmed_spreads
        node._note_echo(h)

        node._retry_unconfirmed_spreads()
        gossip.force_fluff.assert_not_called()

    def test_item_that_never_comes_back_is_flooded(self, node_env):
        node, _, __, gossip, *_ = node_env
        blk = make_block(1, node.cs.tip["hash"], [])

        node._spread(blk, "block", blk["hash"])
        # Pretend the deadline has passed rather than waiting it out.
        item, kind, _ = node._unconfirmed_spreads[blk["hash"]]
        node._unconfirmed_spreads[blk["hash"]] = (item, kind, time.monotonic() - 3600)

        node._retry_unconfirmed_spreads()

        gossip.force_fluff.assert_called_once()
        assert gossip.force_fluff.call_args.args[0] is blk
        # Cleared, so it isn't re-flooded on every subsequent tick.
        assert blk["hash"] not in node._unconfirmed_spreads

    def test_retry_floods_rather_than_stemming_again(self, node_env):
        """The first attempt already spent what privacy a stem buys, and the
        walk has now demonstrably failed once. Another private hand-off
        risks the same silent death; delivery wins on the retry."""
        node, _, __, gossip, *_ = node_env
        blk = make_block(1, node.cs.tip["hash"], [])
        node._spread(blk, "block", blk["hash"])
        item, kind, _ = node._unconfirmed_spreads[blk["hash"]]
        node._unconfirmed_spreads[blk["hash"]] = (item, kind, time.monotonic() - 3600)

        node._retry_unconfirmed_spreads()

        gossip.force_fluff.assert_called_once()
        gossip.spread.assert_called_once()   # only the original, no re-stem

    def test_echo_deadline_uses_measured_times_once_there_are_enough(self, node_env):
        node, *_ = node_env
        assert node._echo_deadline_seconds() == node_mod.ECHO_BOOTSTRAP_SECONDS
        for _ in range(10):
            node._echo_seconds.append(4.0)
        # p95 of the node's own round trips, doubled for the ordinary tail.
        assert node._echo_deadline_seconds() == 8.0
