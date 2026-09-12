"""
Unit tests for tx.py: the plaintext transaction format.

Covers: create, tx_hash, tx_size, tx_size_in_block, validate (fields/
outputs, signature, nonce, balance checks). Fees are sender-bid, so there
is no protocol fee formula to test here.

All tests are pure and local. No network, no chain, no disk.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import crypto
import tx as tx_mod
import state as state_mod
from params import TICKS_PER_LAPSE
from tests.fixtures import keypair, address, pubkey_hex, make_tx, seed_balance


def fresh_state():
    return state_mod.State()


# ---------------------------------------------------------------------------
# 1. Transaction creation
# ---------------------------------------------------------------------------

class TestCreate:
    def test_create_returns_dict_with_required_fields(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        for field in ["from", "pubkey", "outputs", "nonce", "fee", "signature"]:
            assert field in t

    def test_signature_is_hex_string(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        assert isinstance(t["signature"], str)
        bytes.fromhex(t["signature"])  # must not raise

    def test_pubkey_is_hex_string(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        assert isinstance(t["pubkey"], str)
        bytes.fromhex(t["pubkey"])

    def test_from_address_matches_pubkey(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        pk_bytes = bytes.fromhex(t["pubkey"])
        expected_addr = crypto.public_key_to_address(pk_bytes)
        assert t["from"] == expected_addr

    def test_nonce_increments(self):
        s = fresh_state()
        seed_balance(s, 0, 1000.0)
        t1 = make_tx(0, 1, TICKS_PER_LAPSE, s)
        s.apply_tx(t1)
        t2 = make_tx(0, 1, TICKS_PER_LAPSE, s)
        assert t2["nonce"] == t1["nonce"] + 1


# ---------------------------------------------------------------------------
# 2. tx_hash
# ---------------------------------------------------------------------------

class TestTxHash:
    def test_hash_returns_64_char_hex(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        h = tx_mod.tx_hash(t)
        assert isinstance(h, str) and len(h) == 64

    def test_hash_is_deterministic(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        assert tx_mod.tx_hash(t) == tx_mod.tx_hash(t)

    def test_different_txs_have_different_hashes(self):
        s = fresh_state()
        seed_balance(s, 0, 1000.0)
        t1 = make_tx(0, 1, TICKS_PER_LAPSE, s)
        s.apply_tx(t1)
        t2 = make_tx(0, 1, TICKS_PER_LAPSE, s)
        assert tx_mod.tx_hash(t1) != tx_mod.tx_hash(t2)

    def test_hash_excludes_signature(self):
        """tx_hash covers the signed content only, not the signature itself:
        Falcon signing draws fresh randomness each time, so a re-signed
        identical tx must still hash identically (same logical tx, same
        id) even though its signature bytes differ."""
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        h1 = tx_mod.tx_hash(t)
        t2 = dict(t)
        t2["signature"] = "00" * 100
        h2 = tx_mod.tx_hash(t2)
        assert h1 == h2


# ---------------------------------------------------------------------------
# 3. tx_size and tx_size_in_block
# ---------------------------------------------------------------------------

class TestTxSize:
    def test_tx_size_excludes_signature(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        size = tx_mod.tx_size(t)
        assert isinstance(size, int) and size > 0
        fields_no_sig = {k: v for k, v in t.items() if k != "signature"}
        raw_size = len(crypto.canonical_json(fields_no_sig))
        assert size == raw_size

    def test_tx_size_in_block_first_position_no_comma(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        s0 = tx_mod.tx_size_in_block(t, position=0)
        s1 = tx_mod.tx_size_in_block(t, position=1)
        assert s1 == s0 + 1  # comma added for non-first

    def test_tx_size_positive(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        assert tx_mod.tx_size(t) > 0


# ---------------------------------------------------------------------------
# 4. validate, field / output checks
# ---------------------------------------------------------------------------

class TestValidateFields:
    def test_valid_tx_passes(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        ok, err = tx_mod.validate(t, s)
        assert ok is True, err

    def test_missing_from_field_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        del t["from"]
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "missing field" in err

    def test_empty_outputs_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        t["outputs"] = []
        ok, err = tx_mod.validate(t, s)
        assert ok is False

    def test_output_with_zero_amount_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, 0, s, outputs_override=[{"to": address(1), "amount": 0}])
        ok, err = tx_mod.validate(t, s)
        assert ok is False

    def test_output_with_negative_amount_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, -1, s, outputs_override=[{"to": address(1), "amount": -1}])
        ok, err = tx_mod.validate(t, s)
        assert ok is False

    def test_invalid_recipient_address_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s,
                    outputs_override=[{"to": "not_an_address", "amount": TICKS_PER_LAPSE}])
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "invalid address" in err

    def test_negative_fee_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        t["fee"] = -1
        ok, err = tx_mod.validate(t, s)
        assert ok is False


# ---------------------------------------------------------------------------
# 4b. validate, field whitelist and memo
#
# Nothing before this ever checked for an *unexpected* field, only that
# the required ones were present, so a sender could attach an arbitrarily
# large field under any name and it would validate fine. That made the
# memo cap below pointless on its own: capping one named field is no
# defense while any other name is still wide open.
# ---------------------------------------------------------------------------

class TestValidateFieldWhitelist:
    def test_unexpected_field_rejected(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        t["junk"] = "A" * 5_000_000
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "unexpected field" in err

    def test_memo_field_alone_is_allowed(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s, memo="thanks for lunch")
        ok, err = tx_mod.validate(t, s)
        assert ok is True, err

    def test_no_memo_key_when_blank(self):
        """create() omits the key entirely rather than storing "", so a
        transaction built without a memo is byte-for-byte what it always
        was, unaffected by this feature existing at all."""
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        assert "memo" not in t

    def test_memo_over_cap_rejected(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s, memo="x" * (tx_mod.MAX_MEMO_BYTES + 1))
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "memo exceeds" in err

    def test_memo_exactly_at_cap_allowed(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s, memo="x" * tx_mod.MAX_MEMO_BYTES)
        ok, err = tx_mod.validate(t, s)
        assert ok is True, err

    def test_memo_cap_counts_utf8_bytes_not_characters(self):
        """A multi-byte character can cost several bytes, so the cap has to
        be checked after UTF-8 encoding, not against len(memo)."""
        s = fresh_state()
        seed_balance(s, 0)
        # 4 bytes each in UTF-8 (outside the BMP), so this is one
        # character over the cap in character count but well over it in
        # the bytes actually being checked.
        char_count = tx_mod.MAX_MEMO_BYTES // 4 + 1
        memo = "\U0001F600" * char_count
        assert char_count < tx_mod.MAX_MEMO_BYTES  # sanity: not over by length alone
        t = make_tx(0, 1, TICKS_PER_LAPSE, s, memo=memo)
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "memo exceeds" in err

    def test_memo_null_byte_rejected(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s, memo="hi\x00there")
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "null byte" in err

    def test_non_string_memo_rejected(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        t["memo"] = 12345
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "memo must be a string" in err

    def test_outputs_within_cap_allowed(self):
        s = fresh_state()
        seed_balance(s, 0)
        outputs = [{"to": address(1), "amount": 1} for _ in range(tx_mod.MAX_OUTPUTS)]
        t = make_tx(0, 1, 0, s, outputs_override=outputs)
        ok, err = tx_mod.validate(t, s)
        assert ok is True, err

    def test_too_many_outputs_rejected(self):
        """Count, not just content, has to be bounded: each output only
        needs a valid address and a positive amount, so a wall of 1-tick
        outputs costs almost nothing in real balance while still bloating
        the transaction, unlike the whitelist above this doesn't defend
        against."""
        s = fresh_state()
        seed_balance(s, 0)
        outputs = [{"to": address(1), "amount": 1} for _ in range(tx_mod.MAX_OUTPUTS + 1)]
        t = make_tx(0, 1, 0, s, outputs_override=outputs)
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "too many outputs" in err


# ---------------------------------------------------------------------------
# 5. validate, signature check
# ---------------------------------------------------------------------------

class TestValidateSignature:
    def test_wrong_pubkey_for_address_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        _, pk2 = keypair(2)
        t["pubkey"] = pk2.hex()
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "pubkey" in err or "address" in err

    def test_tampered_signature_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        t["signature"] = "00" * 752
        ok, err = tx_mod.validate(t, s)
        assert ok is False

    def test_non_hex_signature_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        t["signature"] = 12345
        ok, err = tx_mod.validate(t, s)
        assert ok is False


# ---------------------------------------------------------------------------
# 6. validate, nonce check (sequential, per sender)
# ---------------------------------------------------------------------------

class TestValidateNonce:
    def test_correct_nonce_passes(self):
        s = fresh_state()
        seed_balance(s, 0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        ok, err = tx_mod.validate(t, s)
        assert ok is True, err

    def test_nonce_too_high_fails(self):
        s = fresh_state()
        seed_balance(s, 0, 1000.0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s, nonce_override=5)
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "nonce" in err

    def test_nonce_already_used_fails(self):
        s = fresh_state()
        seed_balance(s, 0, 1000.0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        s.apply_tx(t)
        # Replay the same tx (same nonce)
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "nonce" in err


# ---------------------------------------------------------------------------
# 7. validate, balance check (outputs + fee <= balance)
# ---------------------------------------------------------------------------

class TestValidateBalance:
    def test_insufficient_balance_fails(self):
        s = fresh_state()
        seed_balance(s, 0, 0.001)  # nearly nothing
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "insufficient" in err

    def test_exact_balance_passes(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        from_addr = address(0)
        bal = s.get_balance(from_addr)
        outputs = [{"to": address(1), "amount": bal}]
        sk, _ = keypair(0)
        t = tx_mod.create(from_addr, pubkey_hex(0), outputs, 1, 0, sk)
        ok, err = tx_mod.validate(t, s)
        assert ok is True, err

    def test_fee_included_in_required_balance(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        from_addr = address(0)
        bal = s.get_balance(from_addr)
        outputs = [{"to": address(1), "amount": bal}]
        sk, _ = keypair(0)
        t = tx_mod.create(from_addr, pubkey_hex(0), outputs, 1, 1, sk)  # fee=1 tips it over
        ok, err = tx_mod.validate(t, s)
        assert ok is False
        assert "insufficient" in err
