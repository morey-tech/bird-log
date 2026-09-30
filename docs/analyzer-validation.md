# Analyzer validation record

Local validation on September 30, 2026, using Linux x86-64 and Python 3.11.13:

- 24 deterministic analyzer tests passed, covering overlap at the recorder's 30-second boundary, persisted context, replay, transaction rollback, publication recovery, review-write concurrency, retention, storage pressure, failed inputs, schema/settings validation, and health.
- All 15 existing recorder tests passed.
- Workflow lint, Python compilation, dependency consistency, and diff whitespace checks passed. Compose YAML checks verified offline runtime, read-only input/model mounts, and absence of analyzer audio devices.
- Every pinned runtime dependency resolved to a Python 3.11 ARM64-compatible or platform-independent wheel. This is a packaging check, not an ARM64 execution test.

## Real-model smoke test

The `prepare-models` command successfully downloaded the official BirdNET 2.4 acoustic/geographic models with BirdNET 1.1.1. The local adapter used LiteRT 2.0.3 and verified manifest checksums. Provisioning required an upstream download retry, which the library handled.

The first nine seconds of the official repository's `example/soundscape.wav` were split into two recorder-format files of two and seven seconds. The test used Ottawa coordinates, the recording date, `MIN_SCORE=0.1`, and the default 1.5-second hop. This lower score threshold was chosen to exercise multiple results; the deployment default remains 0.5.

With Python IPv4/IPv6 socket connections blocked in the parent and spawned worker processes, the complete `once` command produced:

| Artifact | Count |
| --- | ---: |
| Completed input recordings | 2 |
| Analysis windows | 5 |
| Detections | 6 |
| Encounters with finalized clips | 3 |

All clips were readable mono PCM_16 WAVs at 48 kHz. Replaying the same inputs preserved the counts. The SQLite backup command produced a database with matching counts and a successful `PRAGMA integrity_check`.

The socket guard verifies the Python inference path did not require internet connections. Full container network isolation is configured in Compose but was not exercised here. Temporary model files, audio fixtures, databases, and smoke-test logs were kept outside the repository.

## Still required on the deployment host

Podman is not available in the development environment, and no Raspberry Pi or Blue Yeti was attached. Container builds/startup, ARM64 inference execution, resource use under continuous capture, power/reboot behavior, and the integrated 24-hour run are not validated. Follow [the analyzer operations guide](analyzer.md) and record those results before closing issue #2. This smoke test does not establish species-classification accuracy or Pi throughput.
