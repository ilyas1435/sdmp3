---
name: Android flush restrictions
description: Distinguish Android sync capabilities from SD-card data integrity errors.
---
Android may allow file copies while denying read-only directory descriptors or destination-specific sync operations.

**Why:** A user reported repeated false flush warnings on a good SD card after a fix that handled only return codes from syncfs; descriptor-open restrictions remained unhandled.

**How to apply:** Handle capability and permission restrictions at every stage before falling back to global sync. Do not hide real I/O errors or fallback failures. Include the failing stage and errno in remaining warnings; obtain the full device warning before claiming its cause is confirmed.
