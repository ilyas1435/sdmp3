---
name: Saved-operation recovery
description: Required partial-recovery behavior and the limits of isolated API tests.
---

Loading a saved operation must treat removed or inaccessible source items as unselected, keep the remaining good items selected with their organization, and report the skipped names and count without rejecting the whole load. Do not remove the skipped items from the stored preset unless the user explicitly updates it.

**Why:** The user repeatedly reported that one deleted source prevented restoration of the entire operation, despite isolated endpoint tests passing.

**How to apply:** Verify the real rendered page through its load button and actual selection state, including failing batch requests and a missing source alongside good sources. A successful endpoint response or a test that stubs the selection-application function is not enough to establish that restoration works.
