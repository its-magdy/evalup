---
type: llm
weight: 3
---
The plugin's runner reads traces in v1 only from an OTLP file export (`traces.source: otlp-file`); a Jaeger, Tempo or ClickHouse backend is reserved and refused (exit 3).
PASS if the answer tells this user that exporting to Jaeger alone does not unlock the tool-use / trajectory / cost layers, and names the file export (an OTLP collector writing files, which `discover` sets up) as what does.
FAIL if the answer says or implies that the existing Jaeger export will unlock those layers, e.g. "if your spans follow gen_ai.* conventions, discover picks them up and those layers unlock". (Field test 2026-09-24: help promised exactly that.)
