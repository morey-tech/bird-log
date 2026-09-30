# Recorder setup and operations

The recorder implementation targets Linux and Python 3.11, with a native ARM64 container build on Raspberry Pi OS Lite (64-bit). Its Python tests run with synthetic audio. The Pi/Blue Yeti, container build, USB reconnect, and 24-hour hardware tests below still need to be performed; this repository does not claim those checks have passed.

## Repository layout

```text
compose.yaml                  Shared stack entry point
.env.example                  Host paths and service configuration
services/
  recorder/
    Containerfile
    pyproject.toml            Service package and pinned Python dependencies
    src/birdlog_recorder/
    tests/
deploy/systemd/               Host startup integration
docs/                        Setup and service contracts
data/                        Ignored local runtime data (use an SSD on the Pi)
```

The analyzer lives in `services/analyzer/` (see the [analyzer guide](analyzer.md)); the web implementation will go in `services/web/`. Each service has its own dependencies, tests, and Containerfile. Add their services to the same root Compose file. Shared deployment assets stay in `deploy/`; introduce shared Python packages only when there is actual shared code.

## Prepare the Pi

Record the hardware and software used for validation:

```sh
cat /proc/device-tree/model
uname -m
cat /etc/os-release
podman version
podman info
```

Install Podman, a Compose provider, `crun`, and ALSA utilities using the host package manager. On Raspberry Pi OS:

```sh
sudo apt update
sudo apt install podman podman-compose crun alsa-utils
arecord -l
id
ls -l /dev/snd
```

Run the stack as your regular user with rootless Podman. Ensure that user belongs to the group owning the sound devices (normally `audio`):

```sh
sudo usermod -aG audio "$USER"
```

Log out and back in after changing group membership; reboot before validating the systemd service so its user manager also picks up the new groups. Configure Podman's OCI runtime as `crun` in your existing `~/.config/containers/containers.conf` if necessary:

```toml
[engine]
runtime = "crun"
```

The Compose configuration uses `group_add: [keep-groups]`, which requires `crun` and passes the caller's supplementary groups. See [Podman's group/device documentation](https://docs.podman.io/en/latest/markdown/podman-run.1.html). Choose the native `podman-compose` provider if another installed Compose provider rejects this Podman-specific group value; set `PODMAN_COMPOSE_PROVIDER=podman-compose` consistently in your shell and systemd environment if needed.

The recorder bind-mounts the `/dev/snd` directory so newly created sound-device nodes can be seen after USB reconnect. This configuration targets rootless Podman; it does not grant host permissions the user lacks. Rootful deployment needs its own device-cgroup configuration and is not the default. Do not relabel `/dev/snd` with `:z`; SELinux hosts may need an appropriate device policy, as described in the Podman documentation. No other service should mount sound devices.

## Configure and start

From the repository root:

```sh
cp .env.example .env
mkdir -p data/recordings data/status/recorder
```

For deployment, edit `RECORDINGS_HOST_DIR` and `RECORDER_STATUS_HOST_DIR` in `.env` to absolute directories on the SSD and create those directories with write access for your regular user. Use a dedicated recordings directory: correctly named recorder files there are eligible for retention cleanup. Existing unrelated files are not removed. Keep the SSD mounted at the same path before starting the stack.

```sh
podman compose config
podman compose build recorder
podman compose run --rm recorder list-devices
podman compose run --rm recorder check-device
podman compose up -d recorder
podman compose logs -f recorder
```

`list-devices` prints input-device names, channel capabilities, default sample rate, and host API identifiers. Set `AUDIO_DEVICE_MATCH` to a unique, case-insensitive substring of the desired name. Zero or multiple matches are errors, rather than selecting an arbitrary index. `check-device` validates the configured sample rate, channel count, and 16-bit capture format through PortAudio. Actual stream opening is validated when recording starts.

If the microphone requires stereo capture, choose `left`, `right`, or `downmix` and rerun `check-device`. All modes write mono output. `mono` requests one capture channel; `left`/`right` select one of two channels; `downmix` averages both with widened arithmetic to avoid clipping from integer overflow. No automatic format fallback or resampling is performed.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `AUDIO_DEVICE_MATCH` | `Yeti` | Unique input-device name substring |
| `AUDIO_SAMPLE_RATE` | `48000` | Capture and output sample rate in Hz |
| `AUDIO_CHANNEL_MODE` | `mono` | `mono`, `left`, `right`, or `downmix` |
| `CHUNK_SECONDS` | `30` | Chunk duration, rounded to whole sample frames |
| `RETENTION_HOURS` | `24` | Completed raw-audio retention from capture start |
| `MIN_FREE_DISK_MB` | `1024` | Free-space reserve, in MiB |
| `MAX_RECORDINGS_MB` | `10000` | Recorder-owned audio budget, in MiB |
| `QUEUE_SECONDS` | `5` | Bounded callback queue, rounded up to 1024-frame blocks |
| `CALLBACK_TIMEOUT_SECONDS` | `5` | Maximum interval without an audio callback |
| `RETRY_MAX_SECONDS` | `60` | Maximum exponential retry delay, starting at one second |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `RECORDINGS_HOST_DIR` | `./data/recordings` | Host audio directory |
| `RECORDER_STATUS_HOST_DIR` | `./data/status/recorder` | Host status directory |

Container paths are `/data/recordings` and `/status/recorder/status.json`. Direct Python runs can override them with `RECORDINGS_DIR` and `STATUS_PATH`. Configuration comes from process environment; the Python application itself does not load `.env` (Compose does).

At the default format, raw PCM is about 8.3 GB/day. The 10,000 MiB default audio budget accommodates a 24-hour buffer plus overhead. Configure sufficient total disk capacity for the free-space reserve and future analyzer clips as well. Retention cleanup runs at startup and approximately once per minute while recording. Free space and the tracked audio budget are checked before each block write. The budget uses a conservative per-file header reservation between scans; it may pause slightly before the exact byte limit.

If eligible cleanup does not restore enough capacity, capture pauses and the worker retries. Unexpired recordings are not deleted merely to satisfy the budget. Raw recordings expire whether or not the future analyzer has processed them. Retention is based on capture timestamps, so correct host time matters.

## Recording and analyzer contract

The microphone stream stays open across chunk rotations. The callback copies fixed-size blocks into a bounded queue; the writer handles channel conversion and disk I/O outside the callback.

Completed files have this shape:

```text
/data/recordings/YYYY/MM/DD/YYYYMMDDTHHMMSS.ffffffZ_<32-character-random-id>.wav
```

The timestamp is the UTC capture time of the first sample. The first block uses the PortAudio ADC timestamp relative to callback time; subsequent contiguous samples advance by frame counts. A reported overrun or dropped queue block closes the current chunk and starts a new capture-time segment. The duration of PortAudio-reported loss can be unknown; the recorder does not invent missing sample counts or insert silence to hide gaps.

The writer creates `.partial` files, finalizes and syncs each WAV, then atomically renames it on the same filesystem to `.wav`. Only `.wav` is a downstream publication signal. Filenames include a random ID to avoid collisions across restarts. Graceful shutdown closes the stream, drains queued blocks, and publishes a shorter valid final chunk.

Failed writes leave unpublished partials. On a capture failure, the current partial and queued audio may be discarded; `capture_interrupted` reports that loss. Startup removes orphaned partials in the recorder naming namespace. Retention touches only matching files in matching UTC date directories, never follows symlinked directories, and prunes empty date directories. One supervisor holds `.recorder.lock` to prevent concurrent recorders using the same directory. The internal `worker` command is reserved for the supervisor; operate the service through `run`.

The supervisor retries in a fresh worker process, refreshing PortAudio device enumeration after reconnect. Backoff doubles to the configured maximum and resets after a session lasting at least a minute. Graceful shutdown allows 20 seconds for the worker; a blocked worker is then killed and its partial file recovered on the next start.

## Health and logs

```sh
podman compose ps
podman compose exec recorder birdlog-recorder health
podman compose exec recorder cat /status/recorder/status.json
podman compose logs --since 10m recorder
podman compose stop recorder
podman compose up -d recorder
```

Logs are JSON lines. Events include `capture_start`, `chunk_published`, `audio_gap`, `audio_loss`, `recording_removed`, `capture_interrupted`, `recorder_failed`, and `capture_retry`. Configure host log rotation as part of deployment; the raw-audio budget does not govern container logs.

The atomic status JSON has `schema_version: 1` and Unix-second UTC timestamps. In `recording` state it includes:

- `updated_at`, `session_started_at`, `last_callback_at`, `last_published_at`, and `last_published_path`.
- `chunks_published`, `queued_blocks`, `dropped_frames`, and `host_status_events` (per worker session).
- `free_bytes`, `min_free_bytes`, `recordings_bytes`, and `max_recordings_bytes`.
- `chunk_seconds`, `queue_seconds`, and `callback_timeout_seconds` for health evaluation.

Other states are `initializing`, `unhealthy`, `retrying`, and `stopped`; their status fields may be limited to an error and retry delay. Consumers must handle missing fields. While retrying, status may become stale between retries; it must not be shown as healthy.

The health command exits zero only when recording, heartbeat age is at most 10 seconds, callbacks are recent, storage has capacity, and publication is progressing. First-chunk startup allowance and ongoing publication deadline are `CHUNK_SECONDS + QUEUE_SECONDS + 10` seconds. Compose marks persistent failures unhealthy; microphone and ordinary I/O failures retry inside the service. A permanently blocked filesystem can leave an unhealthy worker requiring operator/container restart.

## Start after reboot

The example user unit expects this repository at `~/bird-log`. Edit its `WorkingDirectory` if installed elsewhere, and ensure `/usr/bin/podman` is correct for your host. For SSD-backed deployment configure the SSD for boot mounting and add `ExecStartPre=/usr/bin/mountpoint -q /absolute/ssd/mount` under `[Service]`. Add `Restart=on-failure` and `RestartSec=10` there if startup should retry until the mount is ready. This checks the actual SSD mount point, not a recordings subdirectory, and prevents a missing SSD from redirecting recordings onto the root filesystem.

```sh
mkdir -p ~/.config/systemd/user
cp deploy/systemd/bird-log.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now bird-log.service
sudo loginctl enable-linger "$USER"
systemctl --user status bird-log.service
journalctl --user -u bird-log.service
```

Build once before enabling the unit. Startup uses `podman compose up -d` with existing local images, so reboot does not require downloading build dependencies. The unit reports stack startup, while Compose/container health reports recorder health. Stop the unit before manual stack shutdown if you want systemd state to reflect that change.

## Tests and hardware validation

Local development (no microphone needed for automated tests):

```sh
python3 -m venv .venv
.venv/bin/pip install -e services/recorder pytest==8.3.5
.venv/bin/pytest services/recorder/tests -q
```

Direct recording additionally requires the host PortAudio and libsndfile libraries. The container installs these libraries itself.

Before considering #1 complete:

1. Record the Pi/OS/architecture, microphone, Podman/provider/runtime versions, storage mount, selected device, and tested format. Build and run the container natively on that Pi.
2. Record for several minutes. Listen to finalized WAVs and inspect their mono/PCM_16/sample-rate headers. Check capture timestamps and sample-count continuity across rotations; confirm that `.partial` files are never consumed as complete audio.
3. Stop gracefully mid-chunk; confirm a playable shorter final WAV. Force a container kill during a chunk, restart, and verify orphan cleanup without publishing a corrupt file.
4. Unplug/reconnect the Yeti and verify unhealthy status, explicit loss logs, rediscovery, and resumed recording without a host reboot. Confirm new device nodes are visible in the rootless container. Name matching must remain unambiguous if the hardware index changes.
5. In a separate test data directory, use a short retention and a small budget; confirm only eligible recordings expire. Set a free-space reserve greater than the test filesystem's free space to simulate low space without filling the SSD. Restore settings and confirm recovery. Test write denial against a disposable directory.
6. Run for 24 hours at production settings. Capture logs and periodic `podman stats --no-stream`, status JSON, directory size, and disk-space observations. Verify no gaps/overruns/dropped frames, scheduled publication, healthy status, and bounded memory/storage. Distinguish this uninterrupted run from deliberate failure tests.
7. Reboot the Pi and verify systemd, SSD mounting, group access, and recording recovery. Repeat startup with internet unavailable after provisioning.

Record results and remaining limitations in the issue; keep hardware criteria open until measured. If the device is missing/ambiguous, rerun `list-devices`; if its format is rejected, test the stereo modes and supported rates. For permission errors check host groups, `crun`, mount ownership, and whether the user manager has refreshed group membership.

API references: [sounddevice stream API](https://python-sounddevice.readthedocs.io/en/0.5.1/api/streams.html) and [soundfile API](https://python-soundfile.readthedocs.io/en/0.13.1/).

## Image builds and publishing

[The container workflow](../.github/workflows/container-build.yml) tests and builds services with a `services/<name>/Containerfile`. The recorder publishes to `ghcr.io/morey-tech/bird-log/recorder` for `linux/amd64` and `linux/arm64`. The analyzer image is also discovered automatically; the web image will join when its Containerfile is added. Service Python tests are run before building when `tests/test_*.py` files exist; add service-specific test steps if a future service uses another test layout or language.

Pushes build services changed across the complete pushed commit range. Pull requests build affected services without registry login or image publishing. Workflow/detection-script changes rebuild every service; removed services are skipped. Builds use per-service GitHub Actions and registry caches, with the default branch updating the registry `buildcache` tag.

Published images receive `sha-<short-commit>` tags. The default branch also updates `latest`. In Actions → Container Build → Run workflow, choose an optional service and custom tag; leaving the service empty builds all images. Invalid service names/tags fail explicitly. `buildcache` is reserved, and `latest` is restricted to the default branch. Publishing uses the repository's `GITHUB_TOKEN` with `packages: write`; no additional registry secret is required. Package visibility and repository access can be managed in GHCR after the first publication.

The local Compose stack still builds from source. To inspect a published multi-platform image after a successful workflow run:

```sh
docker buildx imagetools inspect ghcr.io/morey-tech/bird-log/recorder:latest
```
