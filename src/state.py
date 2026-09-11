"""Balance ledger, nonce tracking, and emission accounting. No disk I/O."""

from params import EMISSION_DECAY_NUMERATOR, EMISSION_DECAY_DENOMINATOR, SUPPLY_CAP


def compute_can_mint(total_minted: int) -> int:
    """Ticks still mintable: SUPPLY_CAP - total_minted, floored at 0.

    Single source of truth for the mintable pool.
    """
    return max(0, SUPPLY_CAP - total_minted)


def compute_reward(total_minted: int) -> int:
    """Single source of truth for block reward. Used by State and NodeView stats.

    Pure integer arithmetic. No floating point, so every node computes
    the exact same reward regardless of platform. See params.py for how
    the decay ratio was derived.
    """
    return (compute_can_mint(total_minted) * EMISSION_DECAY_NUMERATOR) // EMISSION_DECAY_DENOMINATOR


class State:
    def __init__(self):
        self._balances    = {}  # addr -> int (ticks)
        self._nonces      = {}  # addr -> int (last used nonce, 0 = never transacted)
        self.total_minted = 0   # ticks minted via block rewards since genesis

    # ------------------------------------------------------------------
    # Balance and nonce access
    # ------------------------------------------------------------------

    def get_balance(self, addr):
        return self._balances.get(addr, 0)

    def get_nonce(self, addr):
        return self._nonces.get(addr, 0)

    def get_all_balances(self):
        """Every holder's balance (ticks), unsorted. Zero balances are never
        stored, so every entry is a real holder."""
        return list(self._balances.values())

    def get_circulating_supply(self):
        return sum(self._balances.values())

    def credit(self, addr, amount):
        if amount <= 0:
            raise ValueError(f"credit amount must be positive, got {amount}")
        self._balances[addr] = self.get_balance(addr) + amount

    def debit(self, addr, amount):
        if amount <= 0:
            raise ValueError(f"debit amount must be positive, got {amount}")
        bal = self.get_balance(addr)
        if bal < amount:
            raise ValueError(f"debit would make balance negative: {bal} - {amount}")
        self._balances[addr] = bal - amount

    def set_nonce(self, addr, nonce):
        self._nonces[addr] = nonce

    # ------------------------------------------------------------------
    # Transaction application
    # ------------------------------------------------------------------

    def apply_tx(self, tx_dict):
        """Apply a validated transaction. Debits sender (outputs + fee),
        credits recipients, advances nonce. The fee is not credited to
        anyone here: it is collected by the block builder as part of
        the block reward distribution (see chainstate._apply_builder_reward)."""
        sender    = tx_dict["from"]
        total_out = sum(o["amount"] for o in tx_dict["outputs"])
        fee       = tx_dict["fee"]

        self.debit(sender, total_out + fee)
        for out in tx_dict["outputs"]:
            self.credit(out["to"], out["amount"])
        self.set_nonce(sender, tx_dict["nonce"])

    # ------------------------------------------------------------------
    # Emission
    # ------------------------------------------------------------------

    def compute_can_mint(self) -> int:
        """Ticks still available to mint."""
        return compute_can_mint(self.total_minted)

    def compute_block_reward(self) -> int:
        """Compute the reward for the next accepted block."""
        return compute_reward(self.total_minted)

    def apply_reward_distribution(self, distribution):
        """Credit a pre-computed reward distribution.

        distribution: list of (address, amount) pairs.
        Each amount is credited independently. total_minted is incremented
        by the sum actually distributed (may be slightly less than the full
        reward due to integer rounding; remainder stays in can_mint).
        """
        for addr, amount in distribution:
            if amount >= 1:
                self.credit(addr, amount)
                self.total_minted += amount

    # ------------------------------------------------------------------
    # Construction from persisted data
    # ------------------------------------------------------------------

    @classmethod
    def from_snapshot(cls, balances: dict, nonces: dict,
                      total_minted: int) -> "State":
        """Restore a State from persisted data. Replaces direct field assignment."""
        s = cls()
        s._balances    = balances
        s._nonces      = nonces
        s.total_minted = total_minted
        return s

    # ------------------------------------------------------------------
    # Snapshot / restore
    # ------------------------------------------------------------------

    def snapshot(self):
        """Return a copy for use as a rollback probe. Safe because keys are
        interned strings and values are ints, both immutable."""
        s = State()
        s._balances    = self._balances.copy()
        s._nonces      = self._nonces.copy()
        s.total_minted = self.total_minted
        return s

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def all_balances(self):
        return dict(self._balances)

    def all_nonces(self):
        return dict(self._nonces)
