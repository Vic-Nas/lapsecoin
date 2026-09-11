"""Plaintext transaction format: creation, serialization, validation.

A transaction is an ordinary, visible transfer: sender, outputs, a
sequential nonce, and a sender-chosen fee. There is no encryption, no
puzzle, and no separate confirm/resolve step. This mirrors standard
practice (e.g. Bitcoin): fees are a market the sender bids into, and
blocks are built by picking whichever valid, pending transactions pay
the most per byte.
"""

import crypto
from crypto import canonical_json


def create(from_addr, pubkey_hex, outputs, nonce, fee, secret_key_bytes):
    """Build and sign a transaction. Returns tx dict with signature."""
    tx = {
        "from":    from_addr,
        "pubkey":  pubkey_hex,
        "outputs": outputs,
        "nonce":   nonce,
        "fee":     fee,
    }
    msg = crypto.serialize_for_signing(tx)
    sig = crypto.sign(msg, secret_key_bytes)
    tx["signature"] = sig.hex()
    return tx


def tx_hash(tx_dict):
    """Deterministic hash of the tx's signed content, excluding the
    signature itself. Falcon-512 signing draws fresh randomness each time,
    so re-signing an identical tx (e.g. a wallet retry) produces a
    different valid signature, hashing it in would give the same logical
    tx a different id every time it's (re)signed, breaking hash-based
    lookups even though the nonce still prevents any double-spend. This
    mirrors Bitcoin's segwit txid fix for the same malleability class."""
    fields = {k: v for k, v in tx_dict.items() if k != "signature"}
    return crypto.sha256_hex(canonical_json(fields))


def tx_size(tx_dict):
    """Fee-basis size: serialized body excluding the signature field.
    The signature is not under the sender's control so is not priced."""
    fields = {k: v for k, v in tx_dict.items() if k != "signature"}
    return len(canonical_json(fields))


def fee_rate(tx_dict):
    """Fee per fee-basis byte. Shared by mempool eviction and API fee estimates
    so the two can't drift apart."""
    return tx_dict.get("fee", 0) / max(tx_size(tx_dict), 1)


def tx_size_in_block(tx_dict, position=0):
    """Size of tx_dict as it appears serialized inside a block's JSON array.
    Position 0 = first element (no leading comma). Position > 0 adds 1 byte
    for the comma separator between elements.
    Used by block.assemble() to track running block size without re-serializing
    the entire block on every candidate tx.
    """
    size = len(canonical_json(tx_dict))
    return size + (1 if position > 0 else 0)


_REQUIRED_FIELDS = ["from", "pubkey", "outputs", "nonce", "fee", "signature"]


def _check_fields_and_outputs(tx_dict):
    for field in _REQUIRED_FIELDS:
        if field not in tx_dict:
            return False, f"missing field: {field}"
    outputs = tx_dict["outputs"]
    if not isinstance(outputs, list) or not outputs:
        return False, "outputs must be a non-empty list"
    for out in outputs:
        if "to" not in out or "amount" not in out:
            return False, "each output must have 'to' and 'amount'"
        if not isinstance(out["amount"], int) or out["amount"] <= 0:
            return False, "output amounts must be positive integers"
        if not crypto.is_valid_address(out["to"]):
            return False, f"invalid address format: {out['to']!r}"
    fee = tx_dict["fee"]
    if not isinstance(fee, int) or fee < 0:
        return False, "fee must be a non-negative integer"
    if not isinstance(tx_dict["nonce"], int):
        return False, "nonce must be an integer"
    return True, None


def _check_signature(tx_dict):
    pubkey_hex = tx_dict["pubkey"]
    sig_hex    = tx_dict["signature"]
    if not isinstance(pubkey_hex, str) or not isinstance(sig_hex, str):
        return False, "pubkey and signature must be hex strings"
    try:
        pubkey_bytes = bytes.fromhex(pubkey_hex)
        sig_bytes    = bytes.fromhex(sig_hex)
        if crypto.public_key_to_address(pubkey_bytes) != tx_dict["from"]:
            return False, "pubkey does not match from address"
        if not crypto.verify(crypto.serialize_for_signing(tx_dict), sig_bytes, pubkey_bytes):
            return False, "invalid signature"
    except Exception:
        return False, "malformed pubkey or signature"
    return True, None


def _check_nonce(tx_dict, state):
    current = state.get_nonce(tx_dict["from"])
    if tx_dict["nonce"] != current + 1:
        return False, f"bad nonce: expected {current + 1}, got {tx_dict['nonce']}"
    return True, None


def _check_balance(tx_dict, state):
    total_out = sum(o["amount"] for o in tx_dict["outputs"])
    available = state.get_balance(tx_dict["from"])
    required  = total_out + tx_dict["fee"]
    if required > available:
        return False, f"insufficient balance: have {available}, need {required}"
    return True, None


def validate(tx_dict, state):
    """Validate a transaction. Returns (True, None) or (False, error_string).

    state: object with .get_balance(addr), .get_nonce(addr)
    """
    for check, args in (
        (_check_fields_and_outputs, (tx_dict,)),
        (_check_signature,          (tx_dict,)),
        (_check_nonce,              (tx_dict, state)),
        (_check_balance,            (tx_dict, state)),
    ):
        ok, err = check(*args)
        if not ok:
            return False, err
    return True, None
