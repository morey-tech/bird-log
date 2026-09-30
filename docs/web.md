# Web dashboard

The `web` service implements issue #3 using FastAPI, Jinja, locally served CSS/JavaScript,
and the analyzer's version 1 SQLite schema. It works without internet after installation.
It does not run BirdNET or access raw audio, models, audio devices, or the container socket.

## Start the three-service stack

Follow [recorder setup](recorder.md) and [analyzer/model setup](analyzer.md) first. Copy
`.env.example` to `.env`, configure your microphone, coordinates, and SSD paths. Create
all persistent directories before starting rootless Podman:

```sh
mkdir -p data/recordings data/clips data/db data/models data/status/recorder data/status/analyzer data/web-cache
podman compose build
podman compose up -d
podman compose logs -f web
```

Open <http://127.0.0.1:8080>. Existing `deploy/systemd/bird-log.service` starts the whole
Compose stack, including web, after reboot; no additional unit is needed. Stop only the
dashboard with `podman compose stop web`, or the entire stack with `podman compose stop`.
Use `podman compose up -d --build web` to apply changes. No startup dependency on recorder
or analyzer is required: a missing database displays a waiting page and `/status` remains usable.

The container defaults to port 8080 internally. Compose publishes only on host loopback.
For access from your trusted LAN, set `WEB_PUBLISH_ADDRESS` to the Pi's LAN IP (or `0.0.0.0`)
and add that IP and/or hostname to `WEB_ALLOWED_HOSTS`, for example:

```dotenv
WEB_PUBLISH_ADDRESS=192.168.1.50
WEB_ALLOWED_HOSTS=localhost,127.0.0.1,[::1],192.168.1.50,birdpi.local
```

Then visit `http://192.168.1.50:8080`. Keep `127.0.0.1` allowed for the container health
probe. Allowed hosts exclude ports and wildcards. Reviews require a same-origin POST and
a per-browser form token with a SameSite cookie. Proxy headers are disabled; reverse-proxy,
public hosting, TLS termination, and multi-user authentication are outside this MVP.

## Pages and counting

- Overview defaults to today, with species first/last times, recent encounters, hourly counts,
  and recorder/analyzer summaries. Date filters can change the displayed period.
- Encounters defaults to the last seven local dates; filter by dates, exact species key,
  review state, and minimum peak score. Pagination and detail links preserve those filters.
- Details show original detections, a peak score (the highest constituent detection score),
  model version, playback, a spectrogram, and a review form. All review states are reversible.
- Species history shows daily encounter totals over the selected period.
- System shows each service independently, including heartbeat age, queue, throughput,
  last processed/published recording information, and service-measured free disk space (GiB).

Counts are encounters, not individual birds. Each encounter belongs to its start timestamp's
local date/hour. By default all review states, including incorrect and unreviewed, are included;
this is stated beside the filters. Scores are ranking signals, not probabilities.
UTC epoch seconds remain canonical in SQLite. `DISPLAY_TIMEZONE` controls display/date
boundaries. Toronto spring/fall days have 23/25 hours; repeated hours show their UTC offset.
Longer overview periods use daily bars to keep the chart readable. Empty hours/days are included.
Species tables list up to 1,000 species; the species total remains exact even if the table is
truncated. Exact species filters can still retrieve other species.

Overview results and system status refresh every `REFRESH_SECONDS`; filter controls remain
intact, and updates pause while keyboard focus is within the region being replaced. Detail
pages do not refresh, preserving playback and unsaved reviews. Refresh failures keep the
last results visible with a notice. All assets and charts are local; JavaScript is optional
for browsing and form submission.

## Configuration and resource bounds

| Variable | Default | Constraints/purpose |
| --- | --- | --- |
| `WEB_HOST` / `WEB_PORT` | `0.0.0.0` / `8080` | Bind address / port 1–65535; Compose fixes internal port 8080 |
| `WEB_PUBLISH_ADDRESS` / `WEB_PUBLISH_PORT` | `127.0.0.1` / `8080` | Compose host binding |
| `WEB_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | Explicit comma-separated hostnames/IPs |
| `DATABASE_PATH` | `/data/db/bird-log.sqlite3` | Analyzer database, schema 1 |
| `CLIPS_DIR` | `/data/clips` | Read-only finalized WAVs |
| `RECORDER_STATUS_PATH` | `/status/recorder/status.json` | Read-only status |
| `ANALYZER_STATUS_PATH` | `/status/analyzer/status.json` | Read-only status |
| `DISPLAY_TIMEZONE` | `America/Toronto` | IANA timezone |
| `PAGE_SIZE` | `50` | 1–100 encounters/page |
| `REFRESH_SECONDS` | `30` | 10–3600 seconds |
| `SPECTROGRAM_CACHE_DIR` | `/data/web-cache/spectrograms` | Web-owned writable directory |
| `SPECTROGRAM_CACHE_MAX_MB` | `256` | 1–4096 MiB; also capped at 512 images |
| `SPECTROGRAM_CACHE_TTL_SECONDS` | `3600` | 60–86400 seconds |
| `LOG_LEVEL` | `INFO` | DEBUG, INFO, WARNING, ERROR, CRITICAL |

Date queries allow at most 366 inclusive dates, from 1970 through 2099. Pages stop at
10,000; narrow date filters to browse more deeply. SQLite busy waits are 500 ms and queries
have a two-second execution budget. Detection evidence is capped at 1,000 rows per detail.
Form bodies are limited to 4 KiB. Status files are limited to 64 KiB.

Spectrograms use a single nonblocking generation slot; concurrent requests receive a retryable
503. The web process uses one worker and limits concurrent connections/tasks to 64. Clips must be finalized analyzer-owned flat WAV names,
mono 48 kHz PCM16, no larger than 24 MiB; spectrogram generation accepts up to 125 seconds.
Labeled spectrograms are 900×300 PNGs. Source size, modification time, and inode invalidate
cached images. A live source is required even for cache hits; expired metadata never grants
access to a cached image. Cleanup runs on generation and every 60 seconds, enforcing age,
size, count, and source availability. An already-open playback stream can finish if retention
unlinks its clip; subsequent requests report unavailable. HTTP responses use `no-store`.
To clear derived data, stop web, delete only files in its cache directory, and restart web.

These are conservative initial limits, **not measured Pi capacity claims**. Benchmark on the
target Pi, especially concurrent inference and spectrogram generation, before increasing them.

## Database, reviews, and backup

Only the analyzer creates/migrates metadata. Web refuses unsupported schema versions and
never creates a database on requests. A missing/uninitialized database produces a setup/waiting
page rather than a process restart. Each request uses a short-lived SQLite connection and
parameterized SQL. List/count/aggregate queries share a read transaction for a consistent view.
Web writes only `review_status`, `review_updated_at`, and `review_version`; its queries never
load the pending `clip_audio` BLOB. Reviews use atomic compare-and-update on `review_version`.
A stale page returns HTTP 409 with a reload link instead of silently overwriting another review.
The analyzer's existing encounter upserts preserve these fields.

Mount the **database directory**, not just the SQLite file, read/write into both analyzer and
web so SQLite can access the `-wal` and `-shm` files. This is an application-level write
restriction, not a read-only mount. Both services must use compatible host UID/GID permissions;
the provided containers run as root inside the rootless user namespace. Shared mounts use
`:z` for SELinux. Clips and status directories are read-only in web; its own cache uses `:Z`.
Do not apply private relabeling to the shared database. Permission errors display a generic
metadata-unavailable page and log a structured failure; inspect host directory ownership and
journal permissions rather than deleting the database.

Use the [analyzer's SQLite backup command](analyzer.md) for a consistent live database snapshot,
including reviews. Do not copy just the main SQLite file while either service has it open.
For a coordinated database-and-clips snapshot, stop analyzer and web, back up the database and
clips together, then restart both. Restoring metadata requires compatible clips/model configuration;
see the analyzer guide. Spectrograms need no backup.

## Status and health contract

Both existing publishers use JSON `schema_version: 1`, `updated_at` as UTC epoch seconds,
and a string `state`; files are atomically replaced. All byte metrics are bytes, durations are
seconds, and throughput is analysis seconds per audio second (less than one keeps up).
See `services/recorder/src/birdlog_recorder/status.py` and analyzer `service.py` for publishers.

- Recorder heartbeat becomes stale after 10 seconds. Healthy also requires recording state,
  a callback within `callback_timeout_seconds`, a published chunk/session start within
  `chunk_seconds + queue_seconds + 10`, free-space reserve, and raw usage below its budget.
- Analyzer heartbeat becomes stale after `max(15, 3*scan_seconds)`, or `stall_seconds` while
  processing. Healthy requires model readiness, idle/processing state, a scan within the stall
  threshold, and adequate clip/database free space. Idle with zero pending recordings is healthy.
- Minimal analyzer initializing records are initializing for 120 seconds, then stale. Minimal
  error/stopped records are unhealthy until stale after 15 seconds. Missing/malformed/unsupported
  status is unknown; future timestamps beyond one second are stale.
- Unknown/unhealthy/stale recorder status does not make an idle analyzer appear to be recording.
  Service health is shown independently. Disk values come from the actual service data volumes,
  never the web container's root filesystem. Progress timestamps display in the configured timezone and free-space values display in GiB.

`GET /healthz` reports web `alive` separately from database `ready`. Its HTTP 200 liveness response
keeps the dashboard running during database/upstream failures while `ready:false` and a reason
report the problem. Container health uses liveness; `/status` and cached metadata remain useful
when recorder or analyzer stops. Logs are JSON events for failed requests, database contention,
review updates, and spectrogram errors; browser errors omit filesystem paths.

## Synthetic development dataset and checks

```sh
python3 -m venv .venv-web
.venv-web/bin/pip install -c services/web/requirements.lock -e 'services/web[test,browser]'
.venv-web/bin/birdlog-web seed --directory /tmp/birdlog-demo
DATABASE_PATH=/tmp/birdlog-demo/db/bird-log.sqlite3 \
CLIPS_DIR=/tmp/birdlog-demo/clips \
RECORDER_STATUS_PATH=/tmp/birdlog-demo/status/recorder/status.json \
ANALYZER_STATUS_PATH=/tmp/birdlog-demo/status/analyzer/status.json \
SPECTROGRAM_CACHE_DIR=/tmp/birdlog-demo/cache \
.venv-web/bin/birdlog-web run
```

Seed refuses to overwrite an existing database. The dataset has three species across three dates,
all review states, available/missing/expired clips, and separate healthy/stale/unhealthy/malformed
status examples. WAVs are synthetic tones, **not bird recordings**. Status examples age naturally;
copy/rewrite their timestamps for testing fresh states. `schema.sql` is a fixture snapshot only;
requests never execute it. A test checks it against the analyzer's schema.

```sh
.venv-web/bin/python -m pytest services/web/tests -q
.venv-web/bin/playwright install chromium
.venv-web/bin/python services/web/tests/browser_smoke.py
```

The browser smoke uses temporary data and a local server, tests filtering, empty/error states,
playback and seeking, spectrograms, persisted review, and mobile overflow. It checks that no
external assets are requested. Screenshots are written to `/tmp/bird-log-web-desktop.png` and
`/tmp/bird-log-web-mobile.png`. CI runs focused tests before image builds; browser smoke is an
explicit local/integration step and requires browser system libraries.

See [recorded validation results](web-validation.md) for checks completed in development.

## Target hardware validation still required

Build both image architectures, provision models, and run the real recorder/analyzer/web stack
for 24 hours. During the run, browse/filter on desktop and mobile, seek clips, generate repeated
spectrograms, save/reload reviews, and restart web while the other services continue. Stop/restart
each upstream service and confirm its status becomes stale without losing browsing access.

Record analyzer pending count/oldest pending age and throughput before/during requests; record
web RSS, CPU, cache bytes/count, and disk free space at regular intervals. Check that backlog does
not grow continuously, cache stays within configured limits, and memory settles after requests.
Exercise a retention deletion during playback and confirm subsequent requests show unavailable.
Test reboot startup and backup/restore including review persistence. Keep this checklist pending
until results come from the actual Pi; desktop automated tests do not satisfy the hardware milestone.
