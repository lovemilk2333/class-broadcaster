SHELL := /usr/bin/env bash

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
BUILD_DIR := $(ROOT)/build
BIN_DIR := $(ROOT)/bin
SERVER_DIR := $(ROOT)/server
CLIENT_DIR := $(ROOT)/client
UPDATER_DIR := $(ROOT)/updater
# Release identity: edit repo-root version.json keys `server` and `client` separately.
# Legacy single `"version"` is still accepted by scripts (applied to both).
SERVER_VERSION := $(shell python3 -c 'import json; d=json.load(open("$(ROOT)/version.json")); print(d.get("server") or d.get("version") or "0.1.0")' 2>/dev/null || echo 0.1.0)
CLIENT_VERSION := $(shell python3 -c 'import json; d=json.load(open("$(ROOT)/version.json")); print(d.get("client") or d.get("version") or "0.1.0")' 2>/dev/null || echo 0.1.0)
# Back-compat alias (prefer SERVER_VERSION / CLIENT_VERSION in new rules).
VERSION := $(SERVER_VERSION)
# Local build timestamp: yyyy-mm-dd HH:MM:SS.xxxx±xx:xx (4 fractional digits + offset).
# Snapshot once per make invocation (:=) so sync-version and go build share one stamp.
# Override with: make BUILD_DATE='2026-10-05 15:00:00.0000+08:00' ...
BUILD_DATE ?= $(shell python3 $(ROOT)/scripts/build_timestamp.py)
BUILD_DATE := $(BUILD_DATE)
SERVER_VERSION_PKG := lovemilk-class-broadcaster/server/internal/version
# ldflags: outer shell must use double quotes (see build-server-amd64 recipe) so spaces in
# BUILD_DATE stay inside -X '…=yyyy-mm-dd HH:MM:SS…'. Nested single quotes break go.
SERVER_LDFLAGS = -s -w -X '$(SERVER_VERSION_PKG).Version=$(SERVER_VERSION)' -X '$(SERVER_VERSION_PKG).BuildDate=$(BUILD_DATE)'
ZIG := $(HOME)/tools/zig/zig
ZIG_TARGET := aarch64-linux-gnu.2.31
ZSTD ?= zstd
ICON_ICO := $(ROOT)/assets/icons/mkcb.ico
# Avoid `case ...)` inside $(shell): Make treats `)` as end of $(shell ...).
NATIVE_SERVER_ARCH := $(shell u=$$(uname -m); if [ "$$u" = x86_64 ] || [ "$$u" = amd64 ]; then echo amd64; elif [ "$$u" = aarch64 ] || [ "$$u" = arm64 ]; then echo arm64; else echo unsupported; fi)

# Naming:
#   make server / make client     → compile + package releasable artifacts under bin/
#   make build-server / build-client → same (explicit “compile and package” alias)
#   make package-server / package-client → bin/ archives only (expects build/ already present)
#   make client-full / package-client-full → client packages + an extra full .tar.zst
#   make package / make all       → server + client release packages
.PHONY: all package \
	server client client-full build-server build-client \
	package-server package-client package-client-full \
	build-server-amd64 build-server-arm64 \
	server-amd64 server-arm64 \
	frontend-embed frontend-embed-force sync-version \
	updater windows-icon \
	install-server-user-service uninstall-server-user-service clean

all: package

package: server client

# -----------------------------------------------------------------------------
# Public release targets (compile + package → bin/)
# -----------------------------------------------------------------------------

# Releasable server: Linux amd64/arm64 ELF + install zips under bin/.
# `server` and `build-server` are equivalent (compile + package).
server: build-server

build-server: build-server-amd64 build-server-arm64
	@$(MAKE) --no-print-directory package-server

# Releasable client: green zip + .tar.zst update packages under bin/.
# `client` and `build-client` are equivalent (compile + package).
client: build-client

# Same as client, but also emit a full files-v1 .tar.zst beside the incremental one.
client-full:
	@$(MAKE) --no-print-directory build-client CLIENT_FULL=1

build-client: updater
	# Windows wheels/site-packages are cached under build/cache/windows-site-packages
	# (keyed by client/requirements.txt). Pass --refresh-site-packages to force reinstall.
	# Launcher/updater PE icons come from assets/icons/mkcb.ico via windows-icon.
	BUILD_DATE='$(BUILD_DATE)' python3 $(ROOT)/scripts/build_embedded_client.py --root $(ROOT) --output $(BUILD_DIR)/client/lovemilk-class-broadcaster --build-date '$(BUILD_DATE)'
	@$(MAKE) --no-print-directory package-client CLIENT_FULL='$(CLIENT_FULL)' PACKAGE_FULL='$(PACKAGE_FULL)'

# -----------------------------------------------------------------------------
# Version stamp
# -----------------------------------------------------------------------------

# Project server/client version.json from root (optional stamp via BUILD_DATE).
sync-version:
	python3 $(ROOT)/scripts/sync_version_json.py --root $(ROOT) --build-date '$(BUILD_DATE)' --targets server client-source

# -----------------------------------------------------------------------------
# Frontend embed (into server binary)
# -----------------------------------------------------------------------------

# Embedded admin UI is copied into server/cmd/server/web and go:embed'd.
# Stamp file makes rebuild conditional on frontend sources (not every server build).
FRONTEND_EMBED_STAMP := $(SERVER_DIR)/cmd/server/web/.embed-stamp
FRONTEND_SOURCES := \
	$(shell find $(ROOT)/frontend/src $(ROOT)/frontend/public \
		-type f \( -name '*.vue' -o -name '*.ts' -o -name '*.js' -o -name '*.css' -o -name '*.html' -o -name '*.svg' -o -name '*.json' \) 2>/dev/null) \
	$(wildcard $(ROOT)/frontend/index.html) \
	$(wildcard $(ROOT)/frontend/package.json) \
	$(wildcard $(ROOT)/frontend/pnpm-lock.yaml) \
	$(wildcard $(ROOT)/frontend/package-lock.json) \
	$(wildcard $(ROOT)/frontend/vite.config.ts) \
	$(wildcard $(ROOT)/frontend/vite.config.js) \
	$(wildcard $(ROOT)/frontend/tsconfig*.json)

frontend-embed: $(FRONTEND_EMBED_STAMP)

# Force a full frontend rebuild + re-embed even when sources look unchanged.
frontend-embed-force:
	@rm -f $(FRONTEND_EMBED_STAMP)
	@$(MAKE) --no-print-directory frontend-embed

$(FRONTEND_EMBED_STAMP): $(FRONTEND_SOURCES)
	@echo "building embedded frontend (sources changed or embed missing)"
	cd $(ROOT)/frontend && pnpm build
	rm -rf $(SERVER_DIR)/cmd/server/web
	mkdir -p $(SERVER_DIR)/cmd/server/web
	cp -a $(ROOT)/frontend/dist/. $(SERVER_DIR)/cmd/server/web/
	@touch $(FRONTEND_EMBED_STAMP)

# -----------------------------------------------------------------------------
# Compile only (build/ intermediates)
# -----------------------------------------------------------------------------

# Host `strip` only understands the native architecture. Cross-built arm64
# binaries must use Zig/llvm/aarch64-*-strip, otherwise skip (ldflags already -s -w).
# Zig objcopy supports --strip-all / -S, not GNU strip's --strip-unneeded.
define strip-server-binary
	@bin="$(1)"; arch="$(2)"; \
	if [ ! -f "$$bin" ]; then exit 0; fi; \
	host=$$(uname -m); \
	if [ "$$arch" = amd64 ] && { [ "$$host" = x86_64 ] || [ "$$host" = amd64 ]; }; then \
		if command -v strip >/dev/null 2>&1; then strip --strip-unneeded "$$bin" 2>/dev/null || true; fi; \
	elif [ "$$arch" = arm64 ]; then \
		if [ -x "$(ZIG)" ]; then \
			"$(ZIG)" objcopy --strip-all "$$bin" "$$bin.stripped" 2>/dev/null && mv -f "$$bin.stripped" "$$bin" \
			|| rm -f "$$bin.stripped"; \
		elif command -v llvm-strip >/dev/null 2>&1; then \
			llvm-strip --strip-unneeded "$$bin" 2>/dev/null || true; \
		elif command -v aarch64-linux-gnu-strip >/dev/null 2>&1; then \
			aarch64-linux-gnu-strip --strip-unneeded "$$bin" 2>/dev/null || true; \
		elif { [ "$$host" = aarch64 ] || [ "$$host" = arm64 ]; } && command -v strip >/dev/null 2>&1; then \
			strip --strip-unneeded "$$bin" 2>/dev/null || true; \
		fi; \
	fi
endef

build-server-amd64: $(FRONTEND_EMBED_STAMP) sync-version
	@mkdir -p $(BUILD_DIR)/server/amd64
	@echo "server version=$(SERVER_VERSION) build_date=$(BUILD_DATE)"
	cd $(SERVER_DIR) && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="$(SERVER_LDFLAGS)" -o $(BUILD_DIR)/server/amd64/lovemilk-class-broadcaster-server ./cmd/server
	$(call strip-server-binary,$(BUILD_DIR)/server/amd64/lovemilk-class-broadcaster-server,amd64)

build-server-arm64: $(FRONTEND_EMBED_STAMP) sync-version
	@mkdir -p $(BUILD_DIR)/server/arm64
	@echo "server version=$(SERVER_VERSION) build_date=$(BUILD_DATE)"
	@if [ -x "$(ZIG)" ] || command -v "$(ZIG)" >/dev/null 2>&1; then \
		echo "building server-arm64 with Zig ($(ZIG_TARGET))"; \
		cd $(SERVER_DIR) && CGO_ENABLED=1 GOOS=linux GOARCH=arm64 CC='$(ZIG) cc -target $(ZIG_TARGET)' go build -trimpath -ldflags="$(SERVER_LDFLAGS) -linkmode external" -o $(BUILD_DIR)/server/arm64/lovemilk-class-broadcaster-server ./cmd/server; \
	else \
		echo "Zig not found; building pure-Go server-arm64 fallback"; \
		cd $(SERVER_DIR) && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -ldflags="$(SERVER_LDFLAGS)" -o $(BUILD_DIR)/server/arm64/lovemilk-class-broadcaster-server ./cmd/server; \
	fi
	$(call strip-server-binary,$(BUILD_DIR)/server/arm64/lovemilk-class-broadcaster-server,arm64)

# Legacy aliases (compile only).
server-amd64: build-server-amd64
server-arm64: build-server-arm64

windows-icon:
	@test -f $(ICON_ICO) || (echo "missing $(ICON_ICO); generate with scripts/generate_app_icon.py" >&2; exit 2)
	@command -v rsrc >/dev/null || go install github.com/akavel/rsrc@latest
	python3 $(ROOT)/scripts/embed_windows_icon.py --root $(ROOT) --icon $(ICON_ICO) --target all

# Updater identity is stamped with the client version (same release train).
UPDATER_LDFLAGS = -s -w -H=windowsgui -X 'main.Version=$(CLIENT_VERSION)' -X 'main.BuildDate=$(BUILD_DATE)'

updater: windows-icon
	@mkdir -p $(BUILD_DIR)/updater
	@echo "updater version=$(CLIENT_VERSION) build_date=$(BUILD_DATE)"
	cd $(ROOT) && CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -trimpath -ldflags="$(UPDATER_LDFLAGS)" -o $(BUILD_DIR)/updater/lovemilk-class-broadcaster-updater.exe ./updater

# -----------------------------------------------------------------------------
# Package only → bin/ (expects build/ already present)
# -----------------------------------------------------------------------------

package-server:
	@test -f $(BUILD_DIR)/server/amd64/lovemilk-class-broadcaster-server || (echo "Missing server amd64 binary. Run 'make build-server' or 'make server' first." >&2; exit 2)
	@test -f $(BUILD_DIR)/server/arm64/lovemilk-class-broadcaster-server || (echo "Missing server arm64 binary. Run 'make build-server' or 'make server' first." >&2; exit 2)
	@mkdir -p $(BIN_DIR)
	cp $(BUILD_DIR)/server/amd64/lovemilk-class-broadcaster-server $(BIN_DIR)/lovemilk-class-broadcaster-server-linux-amd64
	cp $(BUILD_DIR)/server/arm64/lovemilk-class-broadcaster-server $(BIN_DIR)/lovemilk-class-broadcaster-server-linux-arm64
	cp -f $(BUILD_DIR)/server/amd64/lovemilk-class-broadcaster-server $(BIN_DIR)/lovemilk-class-broadcaster-server-$(SERVER_VERSION)-linux-amd64
	cp -f $(BUILD_DIR)/server/arm64/lovemilk-class-broadcaster-server $(BIN_DIR)/lovemilk-class-broadcaster-server-$(SERVER_VERSION)-linux-arm64
	bash $(ROOT)/scripts/package-server-zip.sh amd64 $(SERVER_VERSION)
	bash $(ROOT)/scripts/package-server-zip.sh arm64 $(SERVER_VERSION)

# Previous install tree used as the incremental .tar.zst base (refreshed after each package-client).
CLIENT_PACKAGE_BASE_DIR := $(BUILD_DIR)/cache/client-package-base
# Set PACKAGE_FULL=1 to force the primary .tar.zst to be full (no incremental).
PACKAGE_FULL ?= 0
# Set CLIENT_FULL=1 (or use make client-full / package-client-full) to also emit a
# separate full files-v1 archive beside the (possibly incremental) primary package.
CLIENT_FULL ?= 0

package-client-full:
	@$(MAKE) --no-print-directory package-client CLIENT_FULL=1

package-client:
	@test -f $(BUILD_DIR)/client/lovemilk-class-broadcaster/lovemilk-class-broadcaster.exe || (echo "Missing Windows client bundle. Run 'make build-client' or 'make client' first." >&2; exit 2)
	@test -f $(BUILD_DIR)/updater/lovemilk-class-broadcaster-updater.exe || (echo "Missing updater PE. Run 'make updater' or 'make client' first." >&2; exit 2)
	@mkdir -p $(BIN_DIR)
	# Green install zip: always full install tree (first-time deploy; NOT accepted by updates/publish).
	python3 $(ROOT)/scripts/package_client_zip.py --input-dir $(BUILD_DIR)/client/lovemilk-class-broadcaster --output $(BIN_DIR)/lovemilk-class-broadcaster-$(CLIENT_VERSION)-windows-amd64.zip
	# Update packages are only .tar.zst (server publish rejects .zip).
	# Prefer incremental against build/cache/client-package-base or the previous green zip.
	# Full client install tree includes the updater PE → components ["updater","client"].
	# CLIENT_FULL=1 → also write *-windows-amd64-full.tar.zst (always --force-full).
	@set -euo pipefail; \
	OUT='$(BIN_DIR)/lovemilk-class-broadcaster-$(CLIENT_VERSION)-windows-amd64.tar.zst'; \
	FULL_OUT='$(BIN_DIR)/lovemilk-class-broadcaster-$(CLIENT_VERSION)-windows-amd64-full.tar.zst'; \
	INPUT='$(BUILD_DIR)/client/lovemilk-class-broadcaster'; \
	BASE_ARGS=(); \
	if [ '$(PACKAGE_FULL)' = '1' ]; then \
		echo 'PACKAGE_FULL=1 → primary .tar.zst is full'; \
		BASE_ARGS+=(--force-full); \
	elif [ -d '$(CLIENT_PACKAGE_BASE_DIR)' ] && [ -n "$$(find '$(CLIENT_PACKAGE_BASE_DIR)' -type f 2>/dev/null | head -n 1)" ]; then \
		echo "incremental base: $(CLIENT_PACKAGE_BASE_DIR)"; \
		BASE_ARGS+=(--base-dir '$(CLIENT_PACKAGE_BASE_DIR)'); \
	else \
		PREV_ZIP=$$(ls -1t '$(BIN_DIR)'/lovemilk-class-broadcaster-*-windows-amd64.zip 2>/dev/null | grep -v '$(CLIENT_VERSION)' | head -n 1 || true); \
		if [ -n "$$PREV_ZIP" ]; then \
			echo "incremental base zip: $$PREV_ZIP"; \
			BASE_ARGS+=(--base-zip "$$PREV_ZIP"); \
		else \
			echo 'no previous base → primary .tar.zst is full'; \
		fi; \
	fi; \
	python3 '$(ROOT)/scripts/package_update.py' --components updater client --version '$(CLIENT_VERSION)' --platform windows-amd64 --input-dir "$$INPUT" --output "$$OUT" --client-version '$(CLIENT_VERSION)' --updater-version '$(CLIENT_VERSION)' --zstd '$(ZSTD)' "$${BASE_ARGS[@]}"; \
	if [ '$(CLIENT_FULL)' = '1' ]; then \
		echo "CLIENT_FULL=1 → also writing $$FULL_OUT"; \
		python3 '$(ROOT)/scripts/package_update.py' --components updater client --version '$(CLIENT_VERSION)' --platform windows-amd64 --input-dir "$$INPUT" --output "$$FULL_OUT" --client-version '$(CLIENT_VERSION)' --updater-version '$(CLIENT_VERSION)' --zstd '$(ZSTD)' --force-full; \
	fi
	python3 $(ROOT)/scripts/package_update.py --component updater --version $(CLIENT_VERSION) --platform windows-amd64 --input-dir $(BUILD_DIR)/updater --output $(BIN_DIR)/lovemilk-class-broadcaster-updater-$(CLIENT_VERSION)-windows-amd64.tar.zst --client-version $(CLIENT_VERSION) --updater-version $(CLIENT_VERSION) --zstd '$(ZSTD)'
	# Refresh incremental base cache from the just-built install tree for the next package-client.
	@rm -rf '$(CLIENT_PACKAGE_BASE_DIR)'
	@mkdir -p '$(CLIENT_PACKAGE_BASE_DIR)'
	@cp -a '$(BUILD_DIR)/client/lovemilk-class-broadcaster/.' '$(CLIENT_PACKAGE_BASE_DIR)/'
	@echo "updated incremental base cache → $(CLIENT_PACKAGE_BASE_DIR)"

# -----------------------------------------------------------------------------
# Install / clean
# -----------------------------------------------------------------------------

install-server-user-service: build-server-$(NATIVE_SERVER_ARCH)
	bash $(ROOT)/scripts/install-server-user-service.sh $(BUILD_DIR)/server/$(NATIVE_SERVER_ARCH)/lovemilk-class-broadcaster-server

uninstall-server-user-service:
	systemctl --user disable --now lovemilk-class-broadcaster-server.service 2>/dev/null || true
	rm -f $(HOME)/.config/systemd/user/lovemilk-class-broadcaster-server.service
	systemctl --user daemon-reload

clean:
	rm -rf $(BUILD_DIR) $(BIN_DIR)
	rm -f $(ROOT)/cmd/client-launcher/rsrc_windows_*.syso $(ROOT)/updater/rsrc_windows_*.syso
	rm -rf $(ROOT)/cmd/client-launcher/winres $(ROOT)/updater/winres
