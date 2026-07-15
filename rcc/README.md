# ROG Control Center integration

The patch in this directory targets `asusctl` commit `52a7eb92` and is licensed
under MPL-2.0, matching the upstream project.

It adds an optional D-Bus tuning-provider adapter. When the provider is absent,
the upstream UI is unchanged. When present, the GPU page renders provider
profiles, detected base/Dynamic Boost/temperature limits, telemetry, clock
offsets and maximum clock. CPU PPT controls use the provider only when the
standard asusctl interface does not expose them.

Apply and build:

```bash
git am 0001-rog-control-center-add-optional-GPU-tuning-provider.patch
cargo build --release -p rog-control-center
```

The backend remains model-specific and owns all validation. The UI treats
capabilities as data and does not assume a 120 W GPU.
