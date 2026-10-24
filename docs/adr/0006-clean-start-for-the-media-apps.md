# ADR 0006: Clean start for the media apps

- Status: accepted (2026-10-23/24)

## Context
The library is re-downloadable. Restoring the *arr databases against a wiped library would trigger a mass search and hit indexer limits.

## Decision
Deploy the media apps with no restored state; only presets (URL bases, auth) are written if absent. The owner re-adds content deliberately.

## Consequences
No accidental mass grabs. Watch history and lists are not carried over.
