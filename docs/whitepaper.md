# LapseCoin: A Peer-to-Peer Electronic Cash System

*Victorio Nascimento*

## Abstract

LapseCoin keeps Bitcoin's consensus model: the chain with the most proven work wins, and the first valid block received is accepted. Proof-of-work is replaced with a Verifiable Delay Function (VDF), tying block production to real elapsed time rather than a race for a lucky hash. Transactions are ordinary and plaintext, with sender-bid fees, much like Bitcoin's own. The main practical difference from Bitcoin is the VDF itself: it is believed to have a much smaller hardware advantage gap than proof-of-work does, so it does not push the network toward the same resource-consumption spiral that proof-of-work mining did.

## 1. Consensus: Verifiable Delay Functions

Each block requires a VDF proof computed over the hash of the previous block and the builder's address. The VDF takes about 120 seconds of strictly sequential computation, and no amount of parallel hardware speeds it up. Binding the builder's address into the challenge stops anyone from copying a broadcast proof and claiming it under their own address: each builder evaluates a different VDF, so a stolen proof verifies against nobody else's challenge.

The transaction list is not part of the VDF challenge. A block rejected for a transaction problem can be fixed and rebroadcast without redoing the 120 seconds of work.

When two chains compete, the one with more cumulative proven VDF iterations wins, not the one with more blocks. A block's iteration count only counts if its VDF proof actually verifies for that many iterations, so it can't be inflated by lying. Ties are routine, not rare: any two builders finishing at the same height require the same protocol-set iteration count, so a simple same-height fork ties exactly. Ties break on the lower VDF output, not the block hash. The block hash includes the transaction list, and since the transaction list is not part of the VDF challenge (see below), a builder can swap it for free after finishing the real work. Breaking ties on block hash would let that same builder grind transaction-list variants after the fact, searching for a lower hash at nearly zero cost, which would undermine the property the VDF exists to enforce in the first place. The VDF output cannot be changed without redoing the actual 120 seconds under a different builder address, so it keeps the tie-break's cost real. A height stays open to a lower-output block for a short window after one is adopted, otherwise whatever arrived first would win once anything was built on top of it, and the tie-break would decide nothing. Work on the next height continues during that window, so it is not a pause.

Rewriting old history means redoing every VDF since that point, sequentially, in as much real time as the honest chain took to produce them. The honest chain keeps advancing the whole time, so the gap only grows.

## 2. Why a VDF instead of proof-of-work

**Proof-of-work has no ceiling; a VDF has a floor instead.** Proof-of-work's reward scales with hash rate with no limit: doubling your hash rate doubles your expected share of every block, forever, which is what drove Bitcoin mining from CPUs to ASICs and an ever-growing energy bill along with it. A VDF flips this. Each block's challenge is a single sequential computation, about 120 seconds here, that no amount of hardware can shrink below. Racing to build a block is not about running more attempts in parallel, it is about how fast one chain of sequential steps evaluates, and that has a hard floor set by the arithmetic itself.

**The floor still leaves a hardware gap, just a bounded one.** Chia Network's 2019 public hardware competition for the same class-group-based VDF construction found specialized implementations beating commodity software by something like 3 to 10 times. That gap caps how much any one generation of hardware can widen the lead, unlike proof-of-work's ASICs, which kept opening a wider lead with each generation instead of converging toward one. Below that top band, a builder does not win a smaller proportional share the way lower hash rate does under proof-of-work: it simply loses outright whenever a faster builder is competing, since it cannot finish in time to even be compared.

**Inside that top band, though, it genuinely is a lottery, and that is by design.** Builders fast enough to finish close together tie routinely rather than rarely, and a tie is broken on whichever VDF output is numerically lowest, a value that is deterministic given the previous block's hash and the builder's own address, but indistinguishable from random across different addresses. Each address that completes a full VDF evaluation for a height gets exactly one such draw. This is also where identity count comes in: nothing in the protocol limits how many addresses one operator runs, and since each address needs its own complete, real evaluation to get a draw, with no way to grind for a better one the way a proof-of-work nonce search allows, running N addresses at the top hardware tier wins exactly N times the single-address share, no more and no less. Splitting one machine's power across several addresses instead does worse, since each fragment then runs too slow to be competitive. Both results were checked against a Monte Carlo model, not assumed.

**That draw is where Sybil resistance is priced, not a coin fee.** The cost of an extra draw is a full VDF's worth of real machine-time, so total participation, and the energy that comes with it, scales linearly with how much an operator spends, the same way Bitcoin's hash rate does, but without the compounding arms race: since the hardware gap is capped, spending more mostly means running more complete machines rather than chasing an ever-widening specialization lead. A coin-denominated registration fee was considered and rejected for the same reason it would not help: the economics here track hardware tier and machine count, not balance, and a fee payable only from an existing balance would lock out the very new, empty-handed node this design means to let in.

**One Sybil surface sits outside this analysis: eclipse attacks on peer discovery**, a node's local view of the network being crowded out by attacker-controlled addresses. That is handled separately, by capping how many peers from the same address subnet a node will admit into its own peer table, not by anything priced in coin.

## 3. Transactions

The base unit is the tick. One LAPSE equals 100,000,000 ticks.

A transaction is a plain, visible dict: a sender address, a public key, a list of outputs (recipient and amount), a sequential per-sender nonce, a fee, and a signature. Nothing about it is encrypted or hidden.

Nonces are sequential per sender, starting from zero: a transaction's nonce must be exactly one more than the sender's last confirmed nonce. This is the standard replay-protection scheme, the same one Bitcoin-style account models use.

Fees are chosen by the sender, not fixed by the protocol. A transaction is valid as long as the sender's balance covers every output plus the fee. Builders are free to prioritize whichever pending transactions pay the most per byte, the same market-based mechanism Bitcoin uses to clear its mempool under load.

Blocks apply their listed transactions in order, checking each one against the state as it stands after the transactions before it in the same block. There is no required canonical ordering across transactions; a block's builder can list them however it likes, as long as each one is individually valid at the point it is applied.

## 4. Fees and block rewards

The builder receives the full block reward for every block, unconditionally, plus every transaction fee in that block. There is no split, and no separate party to pay out to.

## 5. Supply

```
reward(block) = floor((21,000,000 LAPSE - total minted) * (1 - 0.5^(1/5,000,000)))
```

The halflife is about 5,000,000 blocks, roughly 20 years at 2 minutes per block. This smooth curve avoids the instability a hard halving schedule can cause.

## 6. Privacy and networking

Transactions and blocks both propagate through Dandelion routing, so no observer can reliably tell which peer first broadcast a given item. A block's builder address is public by construction, since that is who gets paid, but which machine produced it need not be. The originator re-sends anything that never comes back, so handing an item to one peer is not a gamble.

A node can also advertise a different address than the one it builds with. That has to be a second real key its operator holds, since peers pay advertised addresses. It hides the link between a node and a wallet, not the wallet.

Signatures use FALCON-512, a lattice-based scheme designed to resist quantum computers. Addresses are twelve-word phrases derived from the public key.

Peers find each other through the BitTorrent DHT. A node only connects to peers sharing its genesis block hash. The full chain is kept forever, so balances can always be recomputed from scratch.

## 7. Security and censorship: what this design does and does not solve

Consensus here is longest-chain, exactly like Bitcoin's, just measured in proven VDF iterations instead of hashes. That inherits Bitcoin's security model in full, including its limits.

Ordinary transaction censorship, a single non-majority actor refusing to include some transaction, is defeated the same way it always has been: any other willing participant can include it instead, and ordinary confirmation-depth economics protect against a brief refusal turning into a permanent one.

A genuine, sustained majority attacker is a different matter, and here this design does not claim to do better than Bitcoin. Fork choice cares only about cumulative proven work; it has no way to look at what a chain contains. A majority attacker can always choose to fork from a point before some transaction was ever confirmed, and build an alternative history that simply never confirms it, at exactly the cost of an ordinary reorg attack. No rule enforced at the level of individual blocks or individual transactions can stop this, because the attacker is not breaking any such rule; it is simply choosing not to extend the branch that contains the disliked content. This is a universal property of longest-chain consensus, not a gap specific to this design.

Other inherited limitations:

- As with Bitcoin, a node syncing from scratch cannot cryptographically distinguish the honest chain from an attacker's alternative on its own; it has to trust the network it connects to at least once.
- Whether VDF-solving hardware availability keeps pace with the network's needs is a market question the protocol cannot guarantee, the same as mining hardware availability is for Bitcoin.

## 8. Conclusion

LapseCoin keeps Bitcoin's core guarantee: no trust required, everything verifiable, no authority can reverse a transaction. It replaces proof-of-work with a Verifiable Delay Function, which is believed to narrow the hardware-advantage gap that drove proof-of-work's runaway energy use, without needing a separate rule bolted on to achieve that. Transactions stay ordinary and plaintext, with sender-bid fees. Supply is capped at 21 million LAPSE with smooth decay and no halvings. Censorship resistance beyond ordinary confirmation-depth security is not claimed, because no rule at the block or transaction level can give it against a genuine majority attacker in a longest-chain system.

## References

1. S. Nakamoto, "Bitcoin: A Peer-to-Peer Electronic Cash System," 2008.
2. NIST, "FIPS 206 (Draft): FN-DSA (FALCON)," 2025.
3. G. Fanti et al., "Dandelion: Redesigning the Bitcoin Network for Anonymity," 2018.
4. A. Loewenstern et al., "BEP 44: Storing arbitrary data in the DHT," 2014.
5. D. Boneh et al., "Verifiable Delay Functions," 2018.
6. Chia Network, "Chia Network's Proof of Space and Time VDF Competition Results," 2019.
