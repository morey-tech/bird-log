# Bird Log

A bird monitor for Raspberry Pi, with continuous audio capture and local species analysis. Bird Log identifies bird species locally with BirdNET, saves recordings of detections, and displays activity over time in a web dashboard.

The first goal is a useful species log with playable evidence. Recognizing individual birds is a separate, experimental extension.

## Project status

The recorder, analyzer, and web dashboard implementations, container definitions, shared Podman Compose stack, automated tests, and systemd startup example are available. Raspberry Pi/Blue Yeti validation, container builds on ARM64, and the 24-hour integrated hardware run are still pending.

Start with the [recorder setup guide](docs/recorder.md), then [configure the analyzer and provision its models](docs/analyzer.md) and [open the web dashboard](docs/web.md). Service implementations live under `services/`; `compose.yaml`, `deploy/`, and `docs/` support the complete stack in this repository.

- [`services/recorder/`](services/recorder/) — continuous USB audio capture.
- [`services/analyzer/`](services/analyzer/) — offline inference, SQLite encounters, and retained clips.
- [`services/web/`](services/web/) — offline dashboard, clip playback, spectrograms, and encounter review.
- [`compose.yaml`](compose.yaml) and [`.env.example`](.env.example) — shared deployment configuration.
- [`deploy/systemd/`](deploy/systemd/) — host startup integration.

The sections below describe the overall project design and first milestone.

## Planned hardware and runtime

| Component | Starting choice |
| --- | --- |
| Computer | Raspberry Pi 5 with 4 GB RAM; benchmark an existing Pi 4 before upgrading |
| Operating system | Raspberry Pi OS Lite, 64-bit |
| Microphone | Linux-compatible USB microphone with wind protection |
| Placement | Pi indoors; microphone outside under shelter, away from fans and vents |
| Storage | Local USB SSD for audio and the database |
| Application | Python |
| Containers | Podman Compose, started after reboot by systemd |
| Machine learning | BirdNET 2.4 with LiteRT as the initial model/runtime candidate to validate on ARM64 |

The intended ML foundation is the official [BirdNET Python library](https://github.com/birdnet-team/birdnet). Model and runtime compatibility, inference speed, and continuous operation must be validated on the chosen Raspberry Pi and container image. Model files will be stored locally so capture and inference can continue without internet access after setup.

## Architecture

Three services separate audio capture, inference, and presentation:

| Service | Responsibility | Planned components |
| --- | --- | --- |
| `recorder` | Continuously capture audio and publish completed chunks to a bounded disk queue | `sounddevice`, `soundfile` |
| `analyzer` | Run BirdNET, group detections into encounters, save clips and metadata, and enforce retention | `birdnet`, SQLite |
| `web` | Show recent detections, species history, activity charts, and playable recordings | FastAPI, Jinja templates |

```text
USB microphone
      |
   recorder --> bounded raw-audio queue --> analyzer
                                               |
                                     SQLite + saved clips
                                               |
                                              web
                                               |
                                            browser
```

Only the recorder needs microphone access. Separating capture from analysis and presentation allows inference or dashboard restarts without interrupting recording. The queue must remain bounded if analysis falls behind; overflow handling and dropped-audio reporting are part of the implementation work.

## Processing pipeline

1. Capture continuously at an initial format of 48 kHz, mono, 16-bit audio, in approximately 30-second chunks.
2. Publish completed chunks atomically so the analyzer never reads a partially written file.
3. Analyze each chunk using the selected model's required windows, preserving context across chunk boundaries.
4. Group nearby detections of the same species into an **encounter**, retaining the underlying detection scores.
5. Save a short audio clip around each encounter and write its metadata to SQLite.

Each encounter will include:

- Recording timestamp.
- Scientific and common species names.
- Model score and model version.
- Saved clip path.
- Review status.

The initial deployment will use a location around Ottawa and the recording date to inform likely species. Model scores are ranking signals, not guaranteed probabilities of correctness.

Counts will be labeled **detections** or **encounters**, rather than bird counts: a single bird can generate many recordings.

## Web dashboard

- Species detected today, with first and last detection times.
- A detection timeline and hourly activity chart.
- Click-to-play recordings with spectrograms.
- Manual review: correct, incorrect, or uncertain.
- Recorder health, analysis backlog, and remaining disk space.

## Storage and retention

At 48 kHz, mono, 16-bit PCM, continuous audio requires approximately **8.3 GB per day**, excluding filesystem and file-header overhead:

```text
48,000 samples/second × 2 bytes/sample × 86,400 seconds/day
= 8,294,400,000 bytes/day
```

The initial retention policy is:

| Data | Intended retention |
| --- | --- |
| Raw audio | Rolling 24-hour buffer |
| Detection clips | 90 days |
| Encounter metadata | Indefinite |
| Model files | Kept locally for offline inference |

A hard disk-space limit will take precedence over the normal audio retention windows. Retention and queue bounds are required for the first version, so continuous recording cannot consume all available storage. Metadata will outlive expired clips; the dashboard should indicate when audio is no longer available.

## Deployment considerations

The planned deployment uses Podman Compose with a systemd service to start the stack after reboot. The recorder Compose service, Containerfile, and systemd example are included; see the [recorder guide](docs/recorder.md) for build and startup commands. The analyzer joins the same stack with offline operation and read-only raw/model mounts; the web service joins it with read-only clips/status, shared SQLite access for reviews, and its own spectrogram cache. See the [web guide](docs/web.md) for local/LAN access and complete-stack operations.

The recorder mounts `/dev/snd` for access to the audio devices, including replacement nodes after USB reconnect. For rootless Podman, supplementary audio-group access may require `keep-groups` with the `crun` runtime. Device mappings and permissions must be tested on the target host; see the [Podman run documentation](https://docs.podman.io/en/latest/markdown/podman-run.1.html).

[BirdNET-Pi](https://github.com/orbuskila/BirdNET-Pi) is a related reference for continuous bird identification on Raspberry Pi. Bird Log's chosen model, runtime, and container image will still need their own performance validation.

## First milestone

- [ ] Validate microphone capture and BirdNET inference on the target ARM64 hardware.
- [ ] Implement continuous recording with atomic chunk publication and a bounded queue.
- [ ] Implement analysis, encounter grouping, clip storage, and SQLite metadata.
- [ ] Enforce audio retention and disk-space limits.
- [ ] Provide a browsable species log with playable recordings and health information.
- [ ] Package the three services with Podman Compose and systemd startup.
- [ ] Complete 24 hours of uninterrupted capture, with analysis keeping up with incoming audio.

## Future experiment: individual recognition

Species classification is the practical first version. Recognizing the same individual bird returning requires separate validation; similar-sounding recordings alone do not establish identity.

**Northern Cardinals (*Cardinalis cardinalis*) are the preferred initial test case** for individual-recognition experiments because they are regularly present around the deployment location. Bird Log's detection and analysis remain open to all supported species; this experimental focus does not introduce a cardinal-only filter.

A later experiment could retain selected high-quality cardinal clips and audio embeddings, compare similar call and song types, and validate results against independently recognizable birds across different days. Until validated, results will be labeled **call clusters**, not named individuals.

Research to inform the experiment:

- Ritchison (1988), [Song repertoires and the singing behavior of male Northern Cardinals](https://www.researchgate.net/publication/259439027_Song_repertoires_and_the_singing_behavior_of_male_Northern_Cardinals). Background on cardinal song repertoires and behavioral context, useful when choosing comparable vocalizations for the test dataset.
- Gallego, Martinez-Vargas, and López (2026), [Individual bird identification by modelling temporal structure in bioacoustic embeddings](https://besjournals.onlinelibrary.wiley.com/doi/10.1111/2041-210x.70399). Evaluates sequences of BirdNET embeddings with a lightweight recurrent model, informing experiments that preserve information across multiple song windows.
- Merino Recalde (2023), [pykanto: A python library to accelerate research on wild bird song](https://besjournals.onlinelibrary.wiley.com/doi/10.1111/2041-210X.14155). Tools for organizing, segmenting, exploring, and labeling vocal repertoires, with an individual-identification example using great tits.

These papers provide biological context and candidate methods; individual recognition still needs validation on the local cardinal recordings. For additional background, see this [research on individual bird recognition](https://arxiv.org/abs/1603.07236).

## License

This project is licensed under the [Apache License 2.0](LICENSE). Third-party libraries and model files are subject to their own licenses.
