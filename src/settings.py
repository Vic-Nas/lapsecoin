"""Node-local settings: operator policy, never protocol.

Nothing here is consensus. Every value is a choice one operator makes for
their own node, and no other node can tell or care what it is set to.

Resolution order, highest first:
  1. environment variable
  2. value stored in this node's database
  3. shipped default

The environment comes first so a node can be run with a setting forced for
one launch (a container, a systemd unit, a one-off) without that quietly
rewriting what the operator saved. When a value is forced that way the
settings page shows it as such rather than pretending it can be edited.
"""

import logging
import os

log = logging.getLogger("ec.settings")

ENV_PREFIX = "LAPSECOIN_"


class Setting:
    __slots__ = ("key", "default", "kind", "label", "help", "minimum")

    def __init__(self, key, default, kind=str, label="", help="", minimum=None):
        self.key     = key
        self.default = default
        self.kind    = kind
        self.label   = label
        self.help    = help
        self.minimum = minimum

    @property
    def env_name(self):
        return ENV_PREFIX + self.key.upper()

    def parse(self, raw):
        """Parse and range-check, raising ValueError on anything this
        setting can't hold. One implementation for both sources, so a value
        the settings page would reject can't get in through the environment
        instead."""
        if self.kind is bool:
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        value = self.kind(raw)
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"{self.key} must be at least {self.minimum}")
        return value


# Privacy is off by default deliberately. A node that advertises nothing is
# harder to pay and harder to reach, and most operators want to be findable;
# the ones who don't should have to say so.
PRIVATE_ADDRESS = Setting(
    "private_address", False, bool,
    label="Hide wallet address from peers",
    help="Advertises a separate address instead of the one this node "
         "builds blocks with. The builder address stays public either way, "
         "it has to be, to be paid.",
)

ADVERTISED_ADDRESS = Setting(
    "advertised_address", "", str,
    label="Address to advertise",
    help="Overrides the generated privacy address. Operator/deployment "
         "use only, set via environment; not shown on the settings page.",
)

# How long a height stays open for its draw after we start building on
# top of it. See Node._reorg_to_sibling for what the draw is and why it
# needs a window at all.
#
# The window costs nothing in head start. We are computing the next
# height throughout it, so its length trades only how long we keep
# collecting against how much of our own next-height work we might redo.
# That is a local call, which is why it is a setting and not a constant.
DRAW_WINDOW_SECONDS = Setting(
    "draw_window_seconds", 15.0, float, minimum=0.0,
    label="Draw window (seconds)",
    help="How long a height keeps accepting a better same-height block "
         "after one is adopted. Not a wait, work on the next height "
         "continues throughout.",
)

# ADVERTISED_ADDRESS is deliberately not in ALL: it's an env-only escape
# hatch for operators, not a page field. The page shows the generated
# privacy address (always already there, see Node.ensure_privacy_key)
# and lets the operator flip whether it's used, nothing to type in.
ALL = [PRIVATE_ADDRESS, DRAW_WINDOW_SECONDS]


class Settings:
    """Reads through env -> storage -> default on every access, so a value
    changed from the settings page takes effect without a restart."""

    def __init__(self, storage):
        self.storage = storage

    def get(self, setting):
        raw = os.environ.get(setting.env_name)
        if raw is not None:
            try:
                return setting.parse(raw)
            except (TypeError, ValueError):
                log.warning("[settings] %s is not a valid %s, ignoring",
                            setting.env_name, setting.kind.__name__)
        raw = self.storage.get_meta("setting_" + setting.key)
        if raw is not None:
            try:
                return setting.parse(raw)
            except (TypeError, ValueError):
                log.warning("[settings] stored %s unreadable, using default",
                            setting.key)
        return setting.default

    def set(self, setting, value):
        self.storage.set_meta("setting_" + setting.key, str(value))

    def forced_by_env(self, setting):
        return os.environ.get(setting.env_name) is not None
