# Changelog

## 0.1.0 (unreleased)

- Initial scaffold of the NetBox DCIM provider.
- Package discovery via the `nv_config_manager.dcim` entry point, provider
  factory, settings validation, and an aiohttp-based async HTTP transport skeleton.
- The full `DCIMClient` surface is stubbed by functional area; every operation
  raises `DCIMOperationNotSupportedError` until implemented.
