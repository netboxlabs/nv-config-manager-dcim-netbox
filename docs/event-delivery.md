# Event delivery

Change events originate in NetBox and are **pushed** to NVCM's render service.
This provider never polls.

The companion plugin's `EVENTS_PIPELINE` bridge normalizes each NetBox change into
the provider-neutral `DCIMChangeEvent` envelope and publishes it: to a logger, or
to a NATS subject set by the plugin's `nats_subject`. The render service consumes
the envelope, calls `NetBoxProvider.normalize_event` to validate it, and dispatches
it to the handler registered for its `object_type`.

These values must agree or events are published and never delivered:

| Setting | Where |
| --- | --- |
| `nats_subject` | plugin `PLUGINS_CONFIG`, set from `nvcm.eventSubject` in `deploy/netbox/values.yaml` |
| `nats_stream` | plugin `PLUGINS_CONFIG`, set from `nvcm.eventStream` in `deploy/netbox/values.yaml` |
| `dcim.event_subject` | NVCM installer config; becomes `[nats] dcim_change_subject` |
| `dcim.event_stream` | NVCM installer config; the JetStream stream NVCM provisions |

The default pairing is stream `netbox`, subject `netbox.change`. `nvcm-netbox
verify` reads both back out of the NetBox pod and compares them to the installer
config before it publishes its test event, so a mismatch is a failed check rather
than a render that never happens. A wrong subject is the quiet one: the broker
accepts the publish and no stream stores it. A wrong stream name is loud — the
plugin checks the stream at connect, so every publish raises.

Two more values have to line up, neither of them configuration:

- The plugin publishes `provider: "netbox"`, which must equal this provider's
  `metadata.name` and NVCM's `[dcim] provider`. If they disagree the consumer's
  provider-identity guard drops the event.
- Every envelope carries `contract_version` (`1.0`), which
  `DCIMChangeEvent.from_dict` **exact-matches** — anything else raises
  `DCIMInvalidDataError` and the event is rejected, not degraded. It is a constant
  on both sides (`events_pipeline.CONTRACT_VERSION` in the plugin, the SDK's
  `DCIM_EVENT_CONTRACT_VERSION`), so it is a question of upgrading the plugin and
  the SDK together rather than of configuration.

## Delivery guarantees

The plugin's publisher is a port of the reference Nautobot broker
(`NVIDIA/nautobot-broker-nats`), so a NetBox deployment gives the render service
the same signal a Nautobot one does:

- **JetStream publish with an ack.** The server confirms the message is stored
  before the publish returns. A core NATS publish would confirm only that the
  bytes left the socket — with no stream bound to the subject, indistinguishable
  from success.
- **Retries with a reconnect between attempts** (10 by default, 0s/1s/2s… apart),
  which rides out a broker restart — and, as the chart notes, a NetBox installed
  before NVCM exists. The cost is that a write can block for roughly a minute
  before a batch is given up on; `nats_publish_attempts` tunes it.
- **A stable `Nats-Msg-Id` per envelope**, so a retried batch that partly landed
  is de-duplicated by JetStream rather than rendered twice.
- **One connection**, held open per NetBox worker process and drained at exit.

What neither implementation has is a replay path. A publish that exhausts its
retries is logged (`netbox_nv_config_manager.events`, `ERROR`) and dropped — it is
not re-raised, because an unreachable broker must not fail a user's write to
NetBox — and there is no outbox and no reconciliation sweep behind it. After an
outage longer than the retry budget, NVCM keeps rendering what it last saw until
the affected objects are touched again (or `nvcm_load_dcim` is re-run). That log
line is the only signal that happened.

## Handler fan-out

The provider registers render-event handlers for these native object types
(`events._HANDLERS`); each returns the device renders a change should trigger:

| `object_type` | Affected-device fan-out |
| --- | --- |
| `dcim.device` | the device itself |
| `dcim.interface` / `dcim.rearport` / `dcim.frontport` | the parent device |
| `dcim.cable` | both termination devices |
| `ipam.vrf` | devices with an interface in the VRF |
| `ipam.prefix` | devices in the prefix's scope (site/location/region) |
| `ipam.ipaddress` | the interface's device |
| `extras.configcontext` | devices matching the context's scope (locations/roles/platforms/tenants/tags…) |
| `netbox_bgp.bgpsession` | the session's device |
| `ipam.asn` | every device whose BGP session references the ASN (`local_as` / `remote_as`) |
| `netbox_bgp.bgppeergroup` | every device whose BGP session uses the peer group |
| `netbox_nv_config_manager.overlayassignment` | the assigned device, or the assigned interface's device |
| `netbox_nv_config_manager.deviceconfigstatus` | the device |

Cross-object handlers skip `delete` events (the object can no longer be resolved)
and are best-effort.

Object types with **no NetBox equivalent** are deliberately not registered:
`dcim.cablepath` (NetBox's `CablePath` is internal),
`dcim.deviceredundancygroup` (no NetBox model), and BGP `bgproutinginstance` /
`peerendpoint` (`netbox-bgp` has no routing-instance or peer-endpoint model, as a
session carries its device directly).
