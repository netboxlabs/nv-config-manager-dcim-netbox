# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Deployment support for NVCM + NetBox on Kubernetes.

`plugin.NetBoxInstallerProvider` is the NVIDIA Config Manager installer
provider, registered in the `nv_config_manager.installer.dcim` entry-point
group as `netbox`. The stock `nv-config-manager-installer` deploys NetBox
through it; `nvcm-netbox` adds the NetBox-only commands around that.

Installed as an extra, so the provider wheelhouse that ships into NVCM's pods
does not carry click, textual or the deployment code::

    pip install nv-config-manager-dcim-netbox[installer]
"""

from nv_config_manager_dcim_netbox.installer.tokens import generate_api_token

__all__ = ["generate_api_token"]
