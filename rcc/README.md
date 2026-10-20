# ROG Control Center integration

The patch in this directory now targets `asusctl` 6.4.0 and is licensed under
MPL-2.0, matching the upstream project. It was originally written against
commit `52a7eb92` and was ported to 6.4.0; the only required change was that
6.4.0's `init_property!`/`setup_external!` macros no longer take a type
argument.

Porting was needed because the distro `rog-control-center` 6.4.0 package
replaces this patched build, which is why the CPU wattage sliders disappear
after that update: stock RCC 6.4.0 reads CPU PPT through `asus-armoury`
firmware attributes, and those return `ENODEV` on `HN7306WV` (see
[`docs/FINDINGS.md`](../docs/FINDINGS.md)). Rebuilding with this patch
against 6.4.0 restores the provider-backed CPU PPT controls.

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
