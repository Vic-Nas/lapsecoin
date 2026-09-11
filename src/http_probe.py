"""Background HTTP-reachability prober for known peers.

The peers page wants to link a peer's address to its own web UI, but a
successful UDP handshake (what PeerPool already tracks) says nothing about
whether that peer's HTTP port. Same number, different protocol, possibly
a different firewall rule, actually accepts inbound connections. This
module answers that question directly instead of guessing from unrelated
signals: it periodically makes a real, short-timeout HTTP request to each
known peer and records whether it succeeded.

Deliberately not part of peerpool.py (see that module's own docstring: pure
data, no I/O, no threads). This is the same separation discovery.py already
uses for UDP, probe here, store there.

External interface (called from main.py):
  run(pool, interval=120, timeout=2.5)   blocking, run as daemon thread
"""

import logging
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("ec.http_probe")

# A cheap, always-present, unauthenticated endpoint, just confirms the
# peer's HTTP server on this address answers at all. Response body is
# ignored; only whether the request succeeds matters.
PROBE_PATH = "/api/info"
MAX_WORKERS = 20


def _probe_one(addr, timeout):
    url = f"http://{addr}{PROBE_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def run(pool, interval=120, timeout=2.5):
    """Forever: probe every currently-known peer address once, in parallel
    (bounded by MAX_WORKERS so one round never issues an unbounded burst of
    requests), then sleep. A peer added or removed between rounds is simply
    picked up or dropped on the next one. No separate bookkeeping needed
    since pool.snapshot() is always the current membership."""
    while True:
        addrs = pool.all_addrs()
        if addrs:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool_exec:
                futures = {pool_exec.submit(_probe_one, addr, timeout): addr
                          for addr in addrs}
                for future in futures:
                    addr = futures[future]
                    try:
                        ok = future.result()
                    except Exception:
                        ok = False
                    pool.set_http_reachable(addr, ok)
            log.debug("[http_probe] checked %d peers", len(addrs))
        time.sleep(interval)
