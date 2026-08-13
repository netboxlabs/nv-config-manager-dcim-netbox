# Register the plugin's extra statuses with NetBox.
#
# NVCM writes statuses NetBox does not ship: a device it is building is
# `provisioning`, then `provisioned`. Without this they fail validation and the
# loader silently falls back to `active`, so every device looks fully in service
# and the lifecycle NVCM drives is invisible.
#
# NetBox reads FIELD_CHOICES from its configuration at startup, before plugins
# load, so a plugin cannot register these itself. Baked into the image rather
# than passed through the chart's YAML extraConfig because the values would then
# be a second copy of the plugin's dict, to be updated by hand whenever the
# plugin adds a status.
#
# Lands in /etc/netbox/config/, which NetBox imports every `.py` from. Only
# configuration.py is mounted over by the chart (subPath), so this file survives.
# The `zz-` prefix loads it last, after the chart's configuration.py.
#
# field_choices imports nothing, so this is safe this early.
import sys

from netbox_nv_config_manager.field_choices import FIELD_CHOICES as _PLUGIN_CHOICES

# Merge rather than assign: FIELD_CHOICES set through the chart's extraConfig
# becomes a global on the main configuration module, and this file loads after
# it. A plain assignment would silently drop whatever was configured there.
_main = sys.modules.get("netbox.configuration.configuration")
FIELD_CHOICES = {**getattr(_main, "FIELD_CHOICES", {}), **_PLUGIN_CHOICES}
