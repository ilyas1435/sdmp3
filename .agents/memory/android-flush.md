---
name: Android flush restrictions
description: Distinguish Android sync capabilities from SD-card data integrity errors.
---
Android may allow file copies while denying read-only directory descriptors or destination-specific sync operations. A valid mounted directory can also produce `ENOENT` from the `syncfs` call, so errno alone is insufficient without knowing which syscall stage failed.

**Why:** A user reported repeated false flush warnings on a good SD card after fixes that handled only some `syncfs` errors; Android can reject descriptor access or return `ENOENT` for the sync operation itself.

**How to apply:** Distinguish opening/statting the destination from calling `syncfs`. Fall back quietly for known capability/policy responses, including `ENOENT` returned by `syncfs`; do not suppress `ENOENT` when opening the destination. Preserve real I/O errors and fallback failures. Include stage and errno in remaining warnings.
