# Analyzer setup and operations

The analyzer is the second service in the shared Compose stack. It consumes finalized recorder WAVs, runs local BirdNET inference, stores detections/encounters in SQLite, and publishes playable clips. Implementation lives in `services/analyzer/`; the web service remains a separate future service.

## Prepare models and storage

Use the recorder's host setup first. Merge the analyzer entries from `.env.example` into your existing `.env` without replacing your recorder settings. Set the actual `LATITUDE`, `LONGITUDE`, SSD paths, and `MAX_CLIP_STORAGE_MB`. The example coordinates are central Ottawa, not an inferred microphone location.

Create the configured host directories before building. With the example local paths:

```sh
mkdir -p data/recordings data/clips data/db data/models data/status/analyzer
podman compose build analyzer
podman compose -f compose.yaml -f deploy/compose.models.yaml run --rm analyzer prepare-models
podman compose up -d recorder analyzer
podman compose logs -f analyzer
```

`prepare-models` explicitly enables network access and writes the model directory. It uses the official library to obtain acoustic and geographic BirdNET 2.4 models, copies their English labels, and writes SHA-256 checksums and provenance to `manifest.json`. Keep the models and manifest together. Stop the analyzer before reprovisioning an existing model directory. A failed download can be retried; the manifest is published only after both model/label pairs are ready.

Regular analyzer operation uses `network_mode: none`, mounts models and raw recordings read-only, and has no sound-device access. It uses `birdnet.load_custom` with local files rather than the automatic downloader. Missing, incompatible, or modified model files fail startup clearly. Model files are not bundled in the image or repository.

The initial implementation pins `birdnet==1.1.1` and `ai-edge-litert==2.0.3`. All tested runtime dependencies are constrained in `services/analyzer/requirements.lock`. The Python package's `ml` extra enables inference; base dependencies suffice for deterministic pipeline tests. The container installs the extra and constraints. Model APIs were checked against the [official BirdNET library](https://github.com/birdnet-team/birdnet); its source code is MIT licensed and model files are [CC BY-NC-SA 4.0](https://github.com/birdnet-team/birdnet/blob/main/LICENSE.md).

The root systemd stack unit starts this service alongside the recorder after models and images are provisioned. Retain the recorder guide's SSD mount checks. The existing container-build workflow discovers the analyzer Containerfile automatically and publishes its image at `ghcr.io/morey-tech/bird-log/analyzer`.

## Configuration

| Variable | Initial value | Meaning |
| --- | --- | --- |
| `LATITUDE`, `LONGITUDE` | Required; example is Ottawa | Geographic filtering location |
| `LOCATION_TIMEZONE` | `America/Toronto` | Convert capture timestamps to local seasonal dates |
| `WINDOW_HOP_SECONDS` | `1.5` | Three-second window stride; allowed range 0.5–3 seconds |
| `MIN_SCORE` | `0.5` | Minimum acoustic model score |
| `GEO_MIN_SCORE` | `0.03` | Minimum geographic model score |
| `MAX_SPECIES_PER_WINDOW` | `5` | Highest scoring qualifying species retained per window |
| `ENCOUNTER_GAP_SECONDS` | `3` | Maximum time between same-species detections in an encounter |
| `MAX_ENCOUNTER_SECONDS` | `60` | Maximum encounter duration; allowed range 3–300 seconds |
| `CLIP_PADDING_SECONDS` | `1` | Requested context before/after an encounter |
| `CLIP_RETENTION_DAYS` | `90` | Normal clip retention, based on encounter end |
| `MAX_CLIP_STORAGE_MB` | Required; example `10240` | Hard clip budget in MiB |
| `ANALYZER_MIN_FREE_DISK_MB` | `1024` | Compose setting for free-space reserve on both database and clip volumes |
| `MAX_INPUT_SECONDS` | `120` | Maximum accepted recording duration; allowed range 3–300 seconds |
| `SCAN_SECONDS` | `5` | Queue rescan and idle heartbeat interval |
| `IDLE_FLUSH_SECONDS` | `90` | Close pending context/encounters after input stops arriving |
| `MAX_ATTEMPTS` | `3` | Maximum inference/transient-read attempts for a recording |
| `RETRY_SECONDS` | `10` | Input retry and storage-pause interval |
| `STALL_SECONDS` | `300` | Maximum processing/status age before becoming unhealthy |

Thresholds are provisional tuning values, not accuracy guarantees. Review actual local recordings before adjusting them. The geographic model uses the recording date in four bins per month (1–48), not ISO week numbers. Filtering precedes the per-window species cap. Acoustic scores are ranking signals, not guaranteed probabilities.

The container has fixed paths `/data/recordings`, `/data/clips`, `/data/db/bird-log.sqlite3`, `/models`, and `/status/analyzer/status.json`. Host paths come from `.env`. Direct Python commands can override these using `RECORDINGS_DIR`, `CLIPS_DIR`, `DATABASE_PATH`, `MODEL_DIR`, and `STATUS_PATH`; their reserve setting is `MIN_FREE_DISK_MB`. The Python CLI reads process environment, not `.env` directly.

Use `LOG_LEVEL` (`INFO` by default) to control application logs.

Model identity and inference/grouping settings are fingerprinted in SQLite. Changing those settings for an existing database is rejected to avoid silently mixing incompatible results. For an experiment with different settings, stop the service and use a new database and clip directory. Storage/health settings can change independently. Automatic reanalysis and schema downgrade are outside the MVP.

## Processing contract

Only the recorder's exact timestamp/random-ID `.wav` naming pattern in matching UTC date directories is discovered. `.partial` files, unrelated files, and symlinked inputs are ignored. Inputs are ordered by capture time and stable filename. Inputs discovered after the committed timeline has advanced past them are recorded as failed late/overlapping inputs; they are not silently spliced into older results.

The initial format contract is **48 kHz mono PCM_16 WAV**, matching the recorder default and BirdNET 2.4 input rate. Audio converts to float32 for inference. Other rates/channel counts are explicitly rejected rather than resampled independently at chunk edges. A recording must have positive duration and fit the configured input bound. If you change the recorder sample rate, add and validate continuous resampling before using it with this analyzer.

The pipeline uses three-second windows with a default 1.5-second stride, aligned to the start of each continuous recording segment. Overlap context carries into the next contiguous chunk, including the recorder’s default 30-second boundaries. Distinct overlapping windows can both detect the same call; encounter grouping keeps them linked, and stable window IDs prevent reprocessing duplicates. Window times derive from UTC capture timestamps plus integer sample offsets. Contiguity allows one sample of filename timestamp rounding; larger gaps end the segment. A short final window is zero-padded only when a gap or end-of-input flush closes the segment and recorded samples remain uncovered; its metadata keeps the actual audio end and marks the window padded. Saved clips contain only recorded samples, never that artificial padding.

Detections of the same species are grouped until the configured gap or duration bound. Encounter score is the maximum underlying detection score. All qualifying stored detections retain their original scores and window/source references. An encounter closes after sufficient following context is available, at a recording gap, or at the idle timeout. The `once` command drains currently discovered input and explicitly closes the final segment. A graceful `run` shutdown preserves the checkpoint/open encounters for continuation rather than introducing an artificial recording gap.

The analyzer retains a bounded PCM context buffer in its SQLite checkpoint. Its maximum retained history is `MAX_ENCOUNTER_SECONDS + ENCOUNTER_GAP_SECONDS + 2 * CLIP_PADDING_SECONDS + 6` seconds, plus the bounded current input during processing. This survives raw-file expiration and process restarts. Persisting audio context increases SQLite/WAL write traffic; use the SSD and include that traffic in Pi benchmarks. The input bound and species cap also bound temporary inference and clip-publication work.

## Crash recovery and retention

Inference runs outside a database write transaction. The analyzer then atomically commits the input completion, windows, detections, encounter updates, clip payloads, and audio checkpoint. A failure before commit leaves the input retryable; stable identities and the completion ledger prevent duplicate results after a successful commit. Schema version 1 is created by the analyzer under a single-instance lock; unsupported schemas fail clearly.

Closed encounter audio is temporarily stored in SQLite until the WAV has been finalized, synced, and atomically renamed. Only then is its row marked `available` and the temporary database payload cleared. A crash after rename but before that update is recoverable from the durable payload. Clients must serve only clips with `clip_state='available'`, and still handle deletion races. Startup removes analyzer-owned partial clips, completes interrupted expiry, and marks externally missing available files as `missing`.

Clips normally expire after 90 days. Storage pressure can evict older available clips earlier to preserve the configured budget/reserve. Only analyzer-owned clips are removed; raw audio remains the recorder's responsibility. Metadata and reviews remain indefinitely, with `clip_state='expired'`. If no eligible clip removal can free sufficient capacity, processing pauses and health becomes unhealthy. Database-volume pressure is checked separately when clips and the database live on different filesystems.

The recorder can expire a raw file before analysis. Known missing files and invalid/corrupt inputs receive explicit failure states; the analyzer proceeds to subsequent input. It cannot enumerate recordings that expired before its first discovery. Transient read/inference errors retry in order up to `MAX_ATTEMPTS`, then receive a permanent failure reason. Database and clip-write errors pause the service rather than labeling valid input as corrupt.

## SQLite interface for the future web service

The database uses WAL, `synchronous=FULL`, foreign keys, and a five-second busy timeout. Put it on a local filesystem, not a network share. The analyzer owns schema migrations and inference fields. Web review writes must be short transactions and must not start independent migrations.

| Table | Purpose |
| --- | --- |
| `configuration` | Immutable model/settings fingerprint and JSON provenance |
| `recordings` | Relative input path, capture time, frame count, processing state, attempts, and error |
| `windows` | Stable window ID, actual UTC start/end, padding flag, source references, configuration ID |
| `detections` | Stable detection ID, window/encounter links, species, UTC times, score |
| `encounters` | Species names, times, maximum score, model identity, clip state/path/times, padding limitation, review fields |
| `checkpoint` | Private analyzer context; do not query from dashboard pages |
| `gaps` | Observed capture-time gaps and observation times |

Timestamps are Unix seconds in UTC. Clip paths are relative filenames `<encounter-id>.wav` within `/data/clips`; source paths are relative to `/data/recordings`. Window source references include source ranges in segment sample coordinates. Timestamp/species indexes support the future history views.

New encounters default to `review_status='unreviewed'`. Other allowed values are `correct`, `incorrect`, and `uncertain`. The web service should update `review_status`, `review_updated_at`, and `review_version`, using the version to reject stale edits. Analyzer upserts explicitly exclude review columns. Do not select `clip_audio` or checkpoint blobs for normal dashboard queries. Completed encounters have `state='closed'`; audio may remain unavailable while publication is pending.

## Health, commands, and backup

```sh
podman compose exec analyzer birdlog-analyzer health
podman compose exec analyzer cat /status/analyzer/status.json
podman compose logs --since 10m analyzer
podman compose stop analyzer
podman compose run --rm analyzer once
podman compose up -d analyzer
```

Do not run `once` concurrently with `run`: an advisory lock on the database directory permits only one analyzer writer. The recorder can continue while either command runs.

Status JSON (`schema_version: 1`) includes model readiness/identity, last scan and progress times, pending count/oldest capture time, failed/missing counts, detection/encounter totals, last processed input, analysis seconds per audio second, database size, clip usage, and free bytes for each volume. States are `initializing`, `processing`, `idle`, `retrying`, `unhealthy`, and `stopped`. An empty queue is healthy idle; recorder availability is deliberately separate. A processing heartbeat older than `STALL_SECONDS`, or a stale scan, is unhealthy. A stuck ML call can require operator/container restart; health checks do not themselves restart a stuck process.

Logs are structured JSON for application events. BirdNET/LiteRT may also emit native diagnostic lines. Configure host log rotation and monitor metadata growth separately from the clip budget. The library’s inference session uses one worker/producer and a bounded shared-memory buffer; the Compose service provides 256 MiB `/dev/shm` and a 512 MiB temporary filesystem. Library session logging is limited to warnings/errors, and temporary storage cannot grow onto the host disk without a bound. Native logs and model setup also use that temporary filesystem.

For a consistent database-and-clips backup, stop only the analyzer (and future web review writes), then back up SQLite through its backup API:

```sh
podman compose stop analyzer
podman compose run --rm analyzer backup /data/db/backup.sqlite3
```

The destination must not exist. Copy that backup, the clips directory, model manifest/files, and configuration to backup storage before restarting. For database-only snapshots the CLI uses SQLite's online backup API, not a live copy of the main database file. To restore, stop all database writers, restore the backup and matching clips into fresh directories (without stale WAL/SHM files), point `.env` at those directories, and restart. Do not merge unrelated clip directories or configurations.

## Tests and hardware acceptance

See the [local validation record](analyzer-validation.md) for completed checks and remaining hardware work.

Run deterministic tests without downloading models:

```sh
python3 -m venv .venv-analyzer
.venv-analyzer/bin/pip install -e services/analyzer pytest==8.3.5
.venv-analyzer/bin/pytest services/analyzer/tests -q
```

For direct model testing, install `services/analyzer[ml]` using `-c services/analyzer/requirements.lock`, provision a local model directory, and set deployment variables. Use a finalized recorder WAV for the repeatable benchmark:

```sh
podman compose run --rm analyzer benchmark /data/recordings/YYYY/MM/DD/RECORDING.wav
```

The benchmark reports audio duration, elapsed inference time, seconds per audio second, peak parent-process RSS, model identity, and detected scores. It uses the sample's capture date when its filename follows the recorder contract. Include container-level `podman stats --no-stream` because the parent RSS does not include all ML worker memory. A ratio below 1 is necessary to keep up; measure sustained throughput while recording and later while browsing the dashboard.

Before closing #2, build/run on the actual ARM64 Pi, verify real-model results against representative recordings, confirm operation with network disabled, measure sustained throughput/memory/storage, test expired inputs and service restarts, and complete the 24-hour integrated capture/analysis run. Automated fixtures validate pipeline behavior, not species-classification accuracy. ARM64 wheel availability alone does not establish runtime or microphone compatibility.
