# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Deployment orchestration for NVCM + NetBox on Kubernetes.

Brings up NetBox in-cluster as NVIDIA Config Manager's DCIM provider. The
combined path derives from NVCM's deployer and fills its provider lifecycle
hooks; provider-only and pass-through installer commands remain available.

Installed as an extra, so the provider wheelhouse that ships into NVCM's pods
does not carry click or the deployment code::

    pip install nv-config-manager-dcim-netbox[installer]
"""

from nv_config_manager_dcim_netbox.installer.tokens import generate_api_token

__all__ = ["generate_api_token"]
