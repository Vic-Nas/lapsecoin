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
    __slots__ = ("key", "default", "kind", "label", "help")

    def __init__(self, key, default, kind=str, label="", help=""):
        self.key     = key
        self.default = default
        self.kind    = kind
        self.label   = label
        self.help    = help

    @property
    def env_name(self):
        return ENV_PREFIX + self.key.upper()

    def parse(self, raw):
        if self.kind is bool:
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        return self.kind(raw)


# Privacy is off by default deliberately. A node that advertises nothing is
# harder to pay and harder to reach, and most operators want to be findable;
# the ones who don't should have to say so.
PRIVATE_ADDRESS = Setting(
    "private_address", False, bool,
    label="Hide wallet address from peers",
    help="Advertise a separate address to peers instead of the one this "
         "node builds blocks with. The builder address inside a block is "
         "public by construction (it has to be, to be paid), so this hides "
         "the link between this node's network identity and its wallet, "
         "not the wallet itself.",
)

ADVERTISED_ADDRESS = Setting(
    "advertised_address", "", str,
    label="Address to advertise",
    help="Used when the above is on. Left empty, one is generated and kept "
         "encrypted alongside the real key, so a private node still "
         "advertises something rather than going dark.",
)

ALL = [PRIVATE_ADDRESS, ADVERTISED_ADDRESS]


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
