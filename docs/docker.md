# Docker Installation and Usage

The container runs three things at once:

| Service | Port | URL |
|:--------|:-----|:----|
| Admin panel (always up) | 8000 | http://localhost:8000/admin/ |
| Browser view — noVNC (in-browser) | 6080 | http://localhost:6080/vnc.html |
| Browser view — native VNC client | 5900 | `vnc://localhost:5900` (no password) |

The admin panel stays up independently of the automation daemon, so you can reach it whenever you want. The daemon is supervised and restarts on its own if it exits.

## Quick Start (Pre-built Image — Recommended)

Pre-built production images are published to GitHub Container Registry on every push to `master`.

```bash
docker run --pull always -d \
  --name openoutreach \
  -p 8000:8000 -p 6080:6080 -p 5900:5900 \
  -v openoutreach_db:/app/data \
  -e DJANGO_SUPERUSER_USERNAME=admin \
  -e DJANGO_SUPERUSER_PASSWORD=change-me \
  -e DJANGO_SUPERUSER_EMAIL=admin@example.com \
  --restart unless-stopped \
  ghcr.io/eracle/openoutreach:latest
```

All data (CRM database, cookies, model blobs, embeddings) persists in the `openoutreach_db` Docker volume.

### Configuration

Configure the daemon in one of two ways:

- **Admin panel** — open http://localhost:8000/admin/ and create a `LinkedInProfile`, a `Campaign`, and fill in `Site Configuration` (LLM key). The daemon starts automatically once the required fields are present.
- **`config.json`** — drop a `config.json` into the data volume (`/app/data/config.json`). See `setup/docker_setup.md` for the full field list.

The `DJANGO_SUPERUSER_*` env vars auto-create the admin login on first boot (skipped if it already exists). Omit them and create one manually with `docker exec -it openoutreach python manage.py createsuperuser`.

### Available Tags

| Tag | Description |
|:----|:------------|
| `latest` | Latest build from `master` |
| `sha-<commit>` | Pinned to a specific commit |
| `1.0.0` / `1.0` | Semantic version (when tagged) |

### VNC (Live Browser View)

The container includes a VNC server for watching the automation live. Connect any VNC client to `localhost:5900` (no password).

On Linux with `vinagre`:
```bash
vinagre vnc://127.0.0.1:5900
```

### Stopping & Restarting

```bash
# Find the container
docker ps

# Stop it
docker stop <container-id>

# Restart (data persists in the openoutreach_db volume)
docker run --pull always -d --name openoutreach \
  -p 8000:8000 -p 6080:6080 -p 5900:5900 \
  -v openoutreach_db:/app/data \
  --restart unless-stopped \
  ghcr.io/eracle/openoutreach:latest
```

---

## Build from Source (Docker Compose)

For development or customization, you can build the image locally. The compose file (`local.yml`)
mounts the entire project directory into the container for live code editing.

### Prerequisites

- [Make](https://www.gnu.org/software/make/)
- [Docker](https://www.docker.com/)
- [Docker Compose](https://docs.docker.com/compose/)

### Build & Run

```bash
git clone https://github.com/eracle/OpenOutreach.git
cd OpenOutreach

# Build and start
make up
```

This builds the Docker image from source with `BUILD_ENV=local` (includes test dependencies) and starts the admin panel, the automation daemon, and the VNC server. `make up` prints the three access URLs. A superuser (`admin` / `admin`) is auto-created for the admin panel — change it via `DJANGO_SUPERUSER_PASSWORD` before exposing the panel.

**Note:** The compose file uses `HOST_UID` / `HOST_GID` environment variables (defaulting to 1000)
for file ownership. If your host UID differs from 1000, set them explicitly:

```bash
HOST_UID=$(id -u) HOST_GID=$(id -g) make up
```

### Useful Commands

| Command | Description |
|:--------|:------------|
| `make build` | Build the Docker image without starting |
| `make up` | Build and start the service |
| `make stop` | Stop the running containers |
| `make logs` | Follow application logs |
| `make up-view` | Start + open VNC viewer (Linux, requires `vinagre`) |
| `make view` | Open VNC viewer standalone (requires `vinagre`) |
| `make docker-test` | Run the test suite in Docker |

### Accessing the browser

- **In-browser (no client):** open http://localhost:6080/vnc.html.
- **Native VNC client:** connect to `localhost:5900` (no password). On Linux, `make up-view` / `make view` auto-open `vinagre`.

### Accessing the admin panel

Open http://localhost:8000/admin/ and log in (`admin` / `admin` by default with compose). The panel is served by the container at all times, independent of the automation daemon.

Reaching it from a remote host/IP (e.g. a VPS) instead of `localhost` works with no extra config — the admin's CSRF check is a same-origin double-submit cookie, not an origin allowlist, so there's nothing to set for a different host/IP. Prefer tunneling over SSH and keeping using `localhost` anyway if the panel isn't behind TLS (see `setup/docker_setup.md`).

### Volume Mounts

The pre-built `docker run` command uses a named Docker volume (`openoutreach_db`) mounted at `/app/data` for data persistence (database, config). The compose setup (`local.yml`) mounts the entire repo `.:/app` for live code editing during development.
