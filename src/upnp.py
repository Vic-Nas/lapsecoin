"""Best-effort UPnP port mapping for the public HTTP port.

Purely a convenience: nothing depends on this succeeding, nothing waits
on it, and it never raises out to the caller. If the router doesn't
support UPnP, has it disabled, or the node is behind CGNAT (no public IP
to map to at all), the node runs exactly as it would without this --
just not externally reachable without a manual port forward.
"""

import logging
import threading

log = logging.getLogger("ec.upnp")

DISCOVER_TIMEOUT_MS = 3000


def try_map_port(port, description="LapseCoin"):
    """Fire-and-forget: runs the actual attempt on a daemon thread so
    startup never blocks on router discovery, and never joins it."""
    threading.Thread(target=_map_port, args=(port, description),
                     name="upnp-map", daemon=True).start()


def _map_port(port, description):
    try:
        import miniupnpc
    except ImportError:
        log.debug("[upnp] miniupnpc not available, skipping port mapping")
        return
    try:
        u = miniupnpc.UPnP()
        u.discoverdelay = DISCOVER_TIMEOUT_MS
        if u.discover() < 1:
            log.debug("[upnp] no UPnP-capable router found")
            return
        u.selectigd()
        u.addportmapping(port, "TCP", u.lanaddr, port, description, "")
        log.info("[upnp] mapped external port %d -> %s:%d", port, u.lanaddr, port)
    except Exception as e:
        log.debug("[upnp] port mapping failed (harmless, node runs fine "
                  "without it): %s", e)
