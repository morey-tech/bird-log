# Container builds and publishing

[The container workflow](../.github/workflows/container-build.yml) discovers each
`services/<name>/Containerfile` and builds images for `linux/amd64` and `linux/arm64`.
This workflow applies to all services in the repository.

| Service | Published image |
| --- | --- |
| Recorder | `ghcr.io/morey-tech/bird-log/recorder` |
| Analyzer | `ghcr.io/morey-tech/bird-log/analyzer` |
| Web | `ghcr.io/morey-tech/bird-log/web` |

Before building, CI validates dependency-file discovery and runs each service's Python
tests when `tests/test_*.py` files exist. Test installs use the service's `test` extra and
`constraints.txt`. Additional languages or test layouts need corresponding CI support.

## Triggers and affected services

Pushes build services changed across the complete pushed commit range, except for `dependabot/**` branches, which are validated only through pull requests to avoid duplicate builds. Pull requests build affected services without registry login or image publishing. Workflow/detection-script changes rebuild every service; removed services are skipped. Builds use per-service GitHub Actions and registry caches, with the default branch updating the registry `buildcache` tag.

## Tags, registry access, and manual builds

Published images receive `sha-<short-commit>` tags. The default branch also updates `latest`. In Actions → Container Build → Run workflow, choose an optional service and custom tag; leaving the service empty builds all images. Invalid service names/tags fail explicitly. `buildcache` is reserved, and `latest` is restricted to the default branch. Publishing uses the repository's `GITHUB_TOKEN` with `packages: write`; no additional registry secret is required. Package visibility and repository access can be managed in GHCR after the first publication.

## Local builds and image inspection

The local Compose stack builds from source. See the [recorder](recorder.md),
[analyzer](analyzer.md), and [web](web.md) guides for service setup and runtime configuration. To inspect a published multi-platform image after a successful workflow run:

```sh
SERVICE=recorder # recorder, analyzer, or web
docker buildx imagetools inspect "ghcr.io/morey-tech/bird-log/$SERVICE:latest"
```
