# Web MVP validation

Validated during implementation of issue #3 on an x86-64 development host with Python 3.11.
These results do not certify the Raspberry Pi deployment or complete the 24-hour milestone.

| Check | Result |
| --- | --- |
| Web integration tests | 19 passed: queries/filters/pagination, local-date/DST boundaries, fractional timezone offsets, review persistence/conflicts/CSRF, SQLite contention, missing/schema states, audio ranges/retention/symlinks, spectrogram cache/resource limits, status staleness/startup and input bounds |
| Analyzer storage/review regression tests | 3 passed, including preservation of concurrent review changes during inference |
| Headless Chromium smoke | Passed: filtering, empty/error states, audio playback and seek, spectrogram rendering, review persistence, mobile overflow, no external asset requests |
| Visual inspection | Desktop overview and 390 px mobile system-status layouts inspected; no horizontal page overflow |
| Python wheel | Built successfully with templates, static assets, and fixture schema included |
| Runtime dependencies | Pinned direct dependencies and complete Python 3.11 closure; `pip check` passed; all locked packages downloaded as ARM64-compatible binary wheels |
| Compose | YAML parsed; web has only database, read-only clips/status, and private cache mounts |
| Workflow | `actionlint` passed with only the existing old-linter Ubuntu 26.04 label check excluded; web tests are wired into the dynamic service build matrix |
| Whitespace | `git diff --check` passed |

The test runner currently emits Starlette's deprecation warning for its supported `httpx`
TestClient adapter; it does not affect runtime or test results. Browser tests use Playwright
with Chromium and keep the application's content security policy enabled.

Container builds/runs, real microphone capture, ARM64 execution, target-Pi resource measurements,
and the integrated 24-hour run remain pending because no Podman/Docker runtime or Pi hardware
is available in this workspace. Follow the hardware checklist in [web operations](web.md).
Do not close the hardware acceptance criteria based on these desktop results.
