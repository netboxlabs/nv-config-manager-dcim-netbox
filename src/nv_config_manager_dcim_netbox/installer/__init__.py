# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Deployment orchestration for NVCM + NetBox on Kubernetes.

Brings up NetBox in-cluster as NVIDIA Config Manager's DCIM provider and hands
the rest of the deployment to NVCM's own installer.

Deliberately imports nothing from ``nv_config_manager_installer``: the coupling
is their CLI surface and their config file schema, both of which are far more
stable than their Python internals on a force-pushed branch.

Installed as an extra, so the provider wheelhouse that ships into NVCM's pods
does not carry click or the deployment code::

    pip install nv-config-manager-dcim-netbox[installer]
"""

from nv_config_manager_dcim_netbox.installer.tokens import generate_api_token

__all__ = ["generate_api_token"]
