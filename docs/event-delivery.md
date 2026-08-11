# Event delivery

Change events originate in NetBox and are **pushed** to NVCM's render service.
This provider never polls.

The companion plugin's `EVENTS_PIPELINE` bridge normalizes each NetBox change into
the provider-neutral `DCIMChangeEvent` envelope and publishes it: to a logger, or
to a NATS subject set by the plugin's `nats_subject`. The render service consumes
the envelope, calls `NetBoxProvider.normalize_event` to validate it, and dispatches
it to the handler registered for its `object_type`.

Three values must agree or events are published and silently never delivered:

| Setting | Where |
| --- | --- |
| `nats_subject` | plugin `PLUGINS_CONFIG`, set from `nvcm.eventSubject` in `deploy/netbox/values.yaml` |
| `dcim.event_subject` | NVCM installer config; becomes `[nats] dcim_change_subject` |
| `dcim.event_stream` | NVCM installer config; the JetStream stream NVCM provisions |

The default pairing is stream `netbox`, subject `netbox.change`. `nvcm-netbox
verify` exercises the whole path.

A fourth value has to line up too: the plugin publishes `provider: "netbox"`, which
must equal this provider's `metadata.name` and NVCM's `[dcim] provider`. If they
disagree the consumer's provider-identity guard drops the event.

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
