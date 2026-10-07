# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""NetBox support for NVCM's DSX Air simulations.

NVCM's `SimOrchestrator` creates the simulation and deploys NVCM; the hooks in
`installer.air_hooks` ship NetBox into the sim's kind cluster, install it and
load the simulated fabric. This package holds the pieces those hooks run:
the on-box bring-up (`callback`), the image build (`images`) and the SOCKS
tunnel (`tunnel`, also the `nvcm-netbox air tunnel` command).
"""
