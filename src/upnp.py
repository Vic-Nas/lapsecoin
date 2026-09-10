"""Best-effort UPnP port mapping for the public HTTP port.

Purely a convenience: nothing depends on this succeeding, nothing waits
on it, and it never raises out to the caller. If the router doesn't
support UPnP, has it disabled, or the node is behind CGNAT (no public IP
to map to at all), the node runs exactly as it would without this --
just not externally reachable without a manual port forward.

miniupnpc is intentionally NOT in requirements.txt: it's a C extension
that needs a compiler to build from source, and a source install with no
compiler present would otherwise fail entirely just for this optional
feature. Install it yourself (`pip install miniupnpc`) on a machine that
can build it if you want this to actually attempt a mapping; every
runtime path here already handles it being absent.
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
        # UDP is the actual P2P transport (UDPTransport) on this port number;
        # TCP only serves the web UI. Mapping TCP alone -- the original
        # bug here -- left every peer connection depending purely on hole
        # punching, never actually opening the port the protocol runs on.
        mapped = []
        for proto in ("UDP", "TCP"):
            try:
                u.addportmapping(port, proto, u.lanaddr, port, description, "")
                mapped.append(proto)
            except Exception:
                log.debug("[upnp] %s mapping failed", proto, exc_info=True)
        if mapped:
            log.info("[upnp] mapped external port %d (%s) -> %s:%d",
                     port, "+".join(mapped), u.lanaddr, port)
        else:
            log.debug("[upnp] no protocol could be mapped")
    except Exception as e:
        log.debug("[upnp] port mapping failed (harmless, node runs fine "
                  "without it): %s", e)
