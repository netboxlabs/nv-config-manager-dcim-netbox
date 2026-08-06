# Changelog

## 0.1.0 (unreleased)

- Initial release of the NetBox DCIM provider.
- Package discovery via the `nv_config_manager.dcim` entry point, provider
  factory, settings validation, and an aiohttp-based async HTTP transport
  (GraphQL for reads, REST for writes).
- The full `DCIMClient` surface is implemented against native NetBox by
  functional area — inventory, reference, IPAM/VRF, hosts/MAC, DHCP, ZTP,
  firmware, lifecycle state, secrets, render, Spectrum-X, and InfiniBand. There
  are no `DCIMOperationNotSupportedError` stubs.
- Change-event integration: `register_render_event_handlers` and
  `normalize_event` map NetBox object changes onto the device renders they
  should trigger.
- Provider settings: `server`, `token`, and the optional `public_url`, `verify`
  (TLS; accepts a CA-bundle path), and `timeout` (seconds, default 30).
