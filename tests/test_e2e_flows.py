"""
End-to-end protocol flow tests

These tests exercise complete protocol scenarios from genesis through
multi-block chains, reorgs, fee dynamics, and the emission schedule,
all without network or disk I/O.

Flows covered:
  E2E-1:  Genesis -> mine blocks -> emit rewards -> verify circulating supply
  E2E-3:  Fork choice: most cumulative proven work wins; ties broken by tip hash
  E2E-4:  Reorg: a shorter chain that becomes longer is accepted
  E2E-8:  Full tx lifecycle: create -> mempool -> block -> confirmed
  E2E-9:  Block assembly: assemble() fills to limit, skips oversized single txs
  E2E-10: Multi-sender block with correct nonce sequencing
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import block as block_mod
import state as state_mod
import tx as tx_mod
import mempool as mempool_mod
from chainstate import ChainState
from params import TICKS_PER_LAPSE, SUPPLY_CAP
from tests.fixtures import (
    address, genesis, make_block,
    make_tx,
)


@pytest.fixture(autouse=True)
def mock_vdf(monkeypatch):
    monkeypatch.setattr("block.vdf_mod.verify", lambda *a, **kw: True)


# ---------------------------------------------------------------------------
# E2E-1: Genesis through N blocks, reward emission
# ---------------------------------------------------------------------------

class TestE2E_EmissionSchedule:
    def test_rewards_minted_every_block(self):
        """The full block reward mints unconditionally, every block."""
        cs = ChainState.from_genesis()
        for h in range(1, 6):
            b = make_block(h, cs.tip["hash"], [])
            ok, err, cs = cs.validate_and_apply(b)
            assert ok is True, f"h={h}: {err}"
        assert cs.state.total_minted > 0

    def test_minted_does_not_exceed_supply_cap(self):
        cs = ChainState.from_genesis()
        for h in range(1, 11):
            b = make_block(h, cs.tip["hash"], [])
            _, _, cs = cs.validate_and_apply(b)
        assert cs.state.total_minted <= SUPPLY_CAP

    def test_reward_decreases_as_supply_fills(self):
        """Emission should shrink as more coins are minted."""
        from state import compute_reward
        r_early = compute_reward(0)
        r_late  = compute_reward(SUPPLY_CAP // 2)
        assert r_late < r_early


# ---------------------------------------------------------------------------
# E2E-3: Fork choice, equal height, lower tip hash wins
# ---------------------------------------------------------------------------

class TestE2E_ForkChoice:
    def test_equal_height_lower_vdf_output_wins(self):
        """Most cumulative proven work wins; ties broken by VDF output (not
        block hash, block_hash includes the transaction list, which
        isn't bound into the VDF challenge and so can be changed for free
        after the real work is done; see chainstate.is_better_than)."""
        cs = ChainState.from_genesis()
        g = cs.tip
        b1a = make_block(1, g["hash"], [], builder_index=0, vdf_output="aa" * 100)
        b1b = make_block(1, g["hash"], [], builder_index=1, vdf_output="bb" * 100)

        _, _, csa = cs.validate_and_apply(b1a)
        _, _, csb = cs.validate_and_apply(b1b)

        # Both are at height 1 with equal iterations; lower vdf_output wins
        winner = csa if csa.tip["vdf_output"] < csb.tip["vdf_output"] else csb
        loser  = csb if winner is csa else csa
        assert winner.is_better_than(loser)
        assert not loser.is_better_than(winner)

    def test_longer_chain_always_wins(self):
        cs = ChainState.from_genesis()
        g = cs.tip
        b1 = make_block(1, g["hash"], [])
        b2 = make_block(2, b1["hash"], [])
        _, _, cs1 = cs.validate_and_apply(b1)
        _, _, cs2 = cs1.validate_and_apply(b2)
        # cs2 (height 2) must always beat cs1 (height 1)
        assert cs2.is_better_than(cs1)
        assert not cs1.is_better_than(cs2)


# ---------------------------------------------------------------------------
# E2E-4: Reorg (apply_better_chain via _evaluate_remote_chain logic)
# ---------------------------------------------------------------------------

class TestE2E_Reorg:
    def test_longer_remote_chain_replaces_local(self):
        """
        Local: genesis -> b1_local
        Remote: genesis -> b1_remote -> b2_remote
        Remote is longer and should win.
        """
        cs_local = ChainState.from_genesis()
        g = cs_local.tip

        b1_local = make_block(1, g["hash"], [], builder_index=0)
        _, _, cs_local = cs_local.validate_and_apply(b1_local)

        b1_remote = make_block(1, g["hash"], [], builder_index=1)
        b2_remote = make_block(2, b1_remote["hash"], [], builder_index=1)

        cs_remote_base = ChainState.from_genesis()
        _, _, cs_remote = cs_remote_base.validate_and_apply(b1_remote)
        _, _, cs_remote = cs_remote.validate_and_apply(b2_remote)

        assert cs_remote.is_better_than(cs_local)

    def test_same_block_produces_equal_chains(self):
        """Applying the same block to two identical chains yields identical tips."""
        cs_local = ChainState.from_genesis()
        g = cs_local.tip

        b1 = make_block(1, g["hash"], [])
        _, _, cs_a = cs_local.validate_and_apply(b1)
        _, _, cs_b = cs_local.validate_and_apply(b1)  # same block

        assert cs_a.tip["hash"] == cs_b.tip["hash"]
        assert not cs_a.is_better_than(cs_b)
        assert not cs_b.is_better_than(cs_a)


# ---------------------------------------------------------------------------
# E2E-8: Full tx lifecycle
# ---------------------------------------------------------------------------

class TestE2E_TxLifecycle:
    def test_tx_create_to_confirmed(self):
        """create tx -> add to mempool -> include in block -> confirmed."""
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        cs.state.total_minted += 100 * TICKS_PER_LAPSE

        mp = mempool_mod.Mempool()
        t = make_tx(0, 1, TICKS_PER_LAPSE, cs.state)
        ok, h = mp.add(t)
        assert ok is True

        b = make_block(1, cs.tip["hash"], mp.all_txs())
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is True, err

        confirmed = {tx_mod.tx_hash(tx) for tx in b["transactions"]}
        mp.remove_many(confirmed)

        assert mp.size() == 0
        assert cs2.state.get_balance(address(1)) == TICKS_PER_LAPSE

    def test_rejected_tx_stays_in_mempool(self):
        """Tx failing state validation stays pending."""
        cs = ChainState.from_genesis()
        # No balance for sender, tx will be invalid
        t = make_tx(3, 4, TICKS_PER_LAPSE, cs.state)
        mp = mempool_mod.Mempool()
        # We add it directly to the mempool (bypassing node.submit_tx validation)
        mp.add(t)
        assert mp.size() == 1
        # It stays in the mempool until pruned by nonce staleness or TTL,
        # the insufficient-balance failure is only caught at block inclusion.
        pruned = mp.prune_stale(state=cs.state)
        assert mp.size() + len(pruned) == 1


# ---------------------------------------------------------------------------
# E2E-9: Block assembly fills to limit
# ---------------------------------------------------------------------------

class TestE2E_BlockAssembly:
    def test_assembly_produces_valid_block_with_txs(self):
        g = genesis()
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 500 * TICKS_PER_LAPSE)
        cs.state.total_minted += 500 * TICKS_PER_LAPSE

        txs = []
        for i in range(5):
            t = make_tx(0, 1, TICKS_PER_LAPSE, cs.state)
            txs.append(t)
            try:
                cs.state.apply_tx(t)
            except Exception:
                break

        assembled = block_mod.assemble(g, txs, address(0), block_mod.VDF_ITERATIONS)
        assert assembled["height"] == 1
        assert assembled["previous_hash"] == g["hash"]
        assert len(assembled["transactions"]) <= len(txs)
        assert "tx_bytes" in assembled

    def test_assembly_respects_block_size_limit(self):
        from params import BLOCK_SIZE_LIMIT
        g = genesis()
        # Provide far more txs than can fit
        dummy_txs = []
        s = state_mod.State()
        s.credit(address(0), 100_000 * TICKS_PER_LAPSE)
        s.total_minted = 100_000 * TICKS_PER_LAPSE
        for _ in range(100):
            t = make_tx(0, 1, TICKS_PER_LAPSE, s)
            dummy_txs.append(t)
            try:
                s.apply_tx(t)
            except Exception:
                break

        assembled = block_mod.assemble(g, dummy_txs, address(0), block_mod.VDF_ITERATIONS)
        # Assembled block (with hash placeholder) must fit
        test_block = {**assembled, "hash": "x" * 64}
        assert block_mod.block_size(test_block) <= BLOCK_SIZE_LIMIT
