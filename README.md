# lovemilk class broadcaster

> [!WARNING]
> This software was largely produced with AI assistance. The human author provided the high-level design and reviewed the result; treat it as experimental before classroom production use.

LAN classroom broadcast system: a **teacher PC runs the server**, and **classroom displays run Windows clients**. The server pushes queue / call-out messages over TLS; clients show them on screen and can speak them with offline TTS.

- **Ident / magic:** `MKCB`
- **License:** [BSD 3-Clause](LICENSE)
- **Design notes (Chinese):** [`design/design.md`](design/design.md), [`design/client.md`](design/client.md)

## Architecture

| Piece | Stack | Role |
|-------|--------|------|
| Server | Go | UDP discovery, TLS sessions, SQLite, admin HTTP API + embedded Vue UI, update publish |
| Client | CPython 3.11 + PySide6 + Go launcher | Discover / connect, display + TTS, download & apply updates |
| Updater | Go (separate PE) | Overlay install after client exits; restarts the launcher |
| Admin UI | Vue 3 + TypeScript + Pinia + PrimeVue + Vue Router (hash) | Messages, devices, updates, config, server logs |

### Default ports

| Port | Protocol | Use |
|------|----------|-----|
| **39001** | UDP | Discovery (`req-detect` / `resp-detect`) |
| **39002** | TCP + TLS 1.3 | Client sessions **and** update package download (short `update_download` connections) |
| **39003** | HTTP | Admin API, embedded frontend, SSE — **not** used for client update payloads |

Mutual TLS uses Ed25519 identity certificates. Clients trust the server by SPKI SHA-256 fingerprint (TOFU). Devices are approved by that fingerprint (`client_id`).

## Repository layout

```text
server/          Go server + embedded frontend assets
client/          Python client (uv / CPython 3.11)
updater/         Standalone Go updater
frontend/        Vue admin (Vite); built into the server binary
scripts/         Packaging, version sync, Windows client assembly
design/          Protocol / product design
bin/             Release artifacts (after make)
build/           Intermediate build trees + caches
version.json     Separate `server` and `client` versions
```

## Versioning

Root [`version.json`](version.json):

```json
{
  "server": "0.1.1",
  "client": "0.1.1",
  "build_date": ""
}
```

- `make server` / `make build-server` stamp the **server** version into Linux binaries and zip names.
- `make client` / `make build-client` stamp the **client** version into the Windows green zip and `.tar.zst` updates.
- Each build writes a local `build_date` of the form `yyyy-mm-dd HH:MM:SS.xxxx±xx:xx`.

Legacy single-field `"version"` is still accepted and applied to both sides.

## Build (from a Linux host)

Requirements (typical): Go toolchain, `python3`, `zstd`, Zig optional (arm64 cross), `uv` for Windows client wheels, Node/`pnpm` when rebuilding the admin UI.

```bash
# Full release: server + client packages under bin/
make package
# aliases: make / make all

# Server only (Linux amd64 + arm64 ELF + install zips)
make server          # same as: make build-server

# Windows client only (green zip + update tar.zst + updater-only tar.zst)
make client          # same as: make build-client

# Client packages + an extra full files-v1 .tar.zst (keeps incremental primary too)
make client-full     # or: CLIENT_FULL=1 make client / make package-client-full

# Re-package from an existing build/ tree (no recompile)
make package-server
make package-client
```

| Target | Output under `bin/` |
|--------|---------------------|
| Server | `lovemilk-class-broadcaster-server-<ver>-linux-{amd64,arm64}.zip` |
| Client (green install) | `lovemilk-class-broadcaster-<ver>-windows-amd64.zip` — first deploy only |
| Client (update) | `lovemilk-class-broadcaster-<ver>-windows-amd64.tar.zst` — publishable (incremental when possible) |
| Client (full update) | `lovemilk-class-broadcaster-<ver>-windows-amd64-full.tar.zst` — with `make client-full` / `CLIENT_FULL=1` |
| Updater only | `lovemilk-class-broadcaster-updater-<ver>-windows-amd64.tar.zst` |

The Windows client is assembled without PyInstaller: official CPython 3.11 embeddable runtime + Windows wheels via `uv` + `-O` bytecode + Go GUI launcher. Site-packages are cached under `build/cache/windows-site-packages/`.

### Incremental update packages

- **Green `.zip`:** always a full install tree (not accepted by admin publish).
- **Update `.tar.zst`:** files-v1 overlay package. When a previous install tree exists at `build/cache/client-package-base` (or a previous green zip in `bin/`), `make package-client` packs **only new/changed files**. If the new tree deletes paths present in the base, packaging falls back to a **full** archive (overlay cannot delete files).
- Force the **primary** update package to be full: `PACKAGE_FULL=1 make package-client`.
- Keep incremental primary **and** also emit a full archive: `make client-full` or `CLIENT_FULL=1 make package-client` → `*-windows-amd64-full.tar.zst`.

Publish only `.tar.zst` in the admin UI. The Go updater overlays members onto the install directory and **never replaces `data/`**.

## Deploy

### Server (Linux user service)

```bash
make server
unzip bin/lovemilk-class-broadcaster-server-*-linux-amd64.zip
cd lovemilk-class-broadcaster-server-*-linux-amd64
./install-user-service.sh
```

No root required. Binary lands under `~/.local/share/lovemilk-class-broadcaster/bin`; runtime data under `data/`.

```bash
journalctl --user -u lovemilk-class-broadcaster-server -f
```

From source: `make install-server-user-service`  
Uninstall (keep data): `make uninstall-server-user-service`  
For headless linger: `loginctl enable-linger "$USER"`

Admin UI: open `http://<server>:39003/` (hash routes such as `#/messages`, `#/devices`, `#/updates`, `#/settings`, `#/server`).

### Client (Windows)

1. Unzip the green `*-windows-amd64.zip` on the display PC.
2. Run `lovemilk-class-broadcaster.exe`.
3. Approve the server fingerprint on first connect; optionally enable auto-connect / autostart (HKCU Run by default).
4. Later upgrades: publish a `.tar.zst` from the admin **Updates** page. Clients download over TLS **39002**, verify files-v1, run the updater, and auto-restart.

Download progress is logged at percentage milestones (`>0%`, `25%`, `50%`, `75%`, `>99%`) with `%=… speed=…MiB/s`.

Client and updater both write **JSON** log lines (`time` / `level` / `msg` / `logger` / `source`) that the server accepts as `ClientLog`. The detached updater uses `data/updater.log` (`logger=mkcb.updater`, stamped version/build date); after relaunch those lines are uploaded so the admin device-log view can diagnose apply failures. Windows overlay skips identical files and retries locked DLL replaces. Catch-up will not roll a client back to an older `client_version`. Intentional exits (`user_exit` / `update` / `admin`) do **not** count against listen-mode probe loss while the hub session is offline.

## Admin frontend (development)

```bash
cd frontend
pnpm install   # or npm / yarn — lockfile is pnpm-lock.yaml
pnpm dev       # Vite on :39004, proxies /api → :39003
pnpm build     # vue-tsc + vite; embed into server via make frontend-embed
```

Hash router keeps bookmarkable sections. Tables use a shared `FilterableDataTable` (PrimeVue filters + pagination).

## Client development

See [`client/README.md`](client/README.md). Short version:

```bash
cd client
uv python install 3.11
uv venv --python 3.11
uv sync

# from repo root
uv run --project client python -m client
uv run --project client python -m unittest discover -s client/tests -q
```

## Protocol (sketch)

Binary frame: magic `MKCB` + major/minor + type + length + seq + flags + **BSON** payload. Discovery is UDP-only; business traffic is TLS after fingerprint TOFU. Messages use a priority queue (lower number = higher priority) with delivery receipts (received / displayed / spoken / …). Retention purge defaults to **185 days**.

More detail: [`design/design.md`](design/design.md).

## Security notes

- TOFU fingerprints must be confirmed by an operator; rotation is never silent.
- Client identity on disk is install-bound and encrypted; it is not a high-entropy HSM binding.
- Update packages are verified (zstd magic, tar path safety, files-v1 SHA-256) before overlay.
- Management HTTP does not serve update binaries to clients.

## License

[BSD 3-Clause](LICENSE) © 2026 lovemilk
