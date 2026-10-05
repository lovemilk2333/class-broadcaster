package main

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"database/sql"
	"embed"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/klauspost/compress/zstd"
	_ "modernc.org/sqlite"

	"lovemilk-class-broadcaster/server/internal/broadcast"
	"lovemilk-class-broadcaster/server/internal/config"
	"lovemilk-class-broadcaster/server/internal/discovery"
	"lovemilk-class-broadcaster/server/internal/identity"
	"lovemilk-class-broadcaster/server/internal/protocol"
	"lovemilk-class-broadcaster/server/internal/session"
	"lovemilk-class-broadcaster/server/internal/version"
)

const (
	// Legacy HTTP download path kept only so old admin UIs that bookmarked it get a clear 410.
	updateDownloadPathPrefix = "/api/v1/updates/packages/"
	updateDownloadTTL        = 6 * time.Hour
	updateDownloadDomain     = "MKCB-UPDATE-DOWNLOAD-v1"
)

// frontendFiles 在构建时由 Makefile 用 frontend/dist 填充；服务端二进制因此自带管理控制台。
// 开发环境保留一个最小 index.html，避免直接 go test 时 embed 找不到匹配文件。
//
//go:embed web/*
var frontendFiles embed.FS

type healthResponse struct {
	Status    string `json:"status"`
	Service   string `json:"service"`
	Version   string `json:"version,omitempty"`
	BuildDate string `json:"build_date,omitempty"`
	TLSPort   uint16 `json:"tls_port,omitempty"`
	HTTPPort  uint16 `json:"http_port,omitempty"`
	UDPPort   uint16 `json:"udp_port,omitempty"`
}

func currentHealth(tlsPort, httpPort, udpPort uint16) healthResponse {
	return healthResponse{
		Status: "ok", Service: "class-broadcaster",
		Version: version.Version, BuildDate: version.BuildDate,
		TLSPort: tlsPort, HTTPPort: httpPort, UDPPort: udpPort,
	}
}

// splitRemoteAddr returns peer host and port for logging (supports IPv4/IPv6).
func splitRemoteAddr(remote string) (host, port string) {
	remote = strings.TrimSpace(remote)
	if remote == "" {
		return "", ""
	}
	h, p, err := net.SplitHostPort(remote)
	if err != nil {
		// Bare IP without port (or other forms).
		return remote, ""
	}
	return h, p
}

// requestClientAddr prefers the direct TCP peer; falls back to X-Forwarded-For / X-Real-IP.
func requestClientAddr(c *gin.Context) (ip, port, remote string) {
	remote = ""
	if c.Request != nil && c.Request.RemoteAddr != "" {
		remote = c.Request.RemoteAddr
	}
	ip, port = splitRemoteAddr(remote)
	// If we only see a reverse-proxy loopback address, prefer forwarded client IP.
	if ip == "" || ip == "127.0.0.1" || ip == "::1" {
		if xff := strings.TrimSpace(c.GetHeader("X-Forwarded-For")); xff != "" {
			// First hop is the original client.
			first := strings.TrimSpace(strings.Split(xff, ",")[0])
			if first != "" {
				if fh, fp, err := net.SplitHostPort(first); err == nil {
					ip, port = fh, fp
				} else {
					ip = first
					if port == "" {
						port = ""
					}
				}
			}
		} else if xri := strings.TrimSpace(c.GetHeader("X-Real-IP")); xri != "" {
			if rh, rp, err := net.SplitHostPort(xri); err == nil {
				ip, port = rh, rp
			} else {
				ip = xri
			}
		}
	}
	return ip, port, remote
}

type serverPreferences struct {
	db         *sql.DB
	updatesDir string
	// seqMu only serializes monotonic update seq allocation (read-modify-write on update_counters).
	// Row updates (withdraw, status) use WHERE update_id=? and must not hold a process-wide DB lock.
	seqMu sync.Mutex
}

func openServerPreferences(path string) (*serverPreferences, error) {
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	// WAL allows concurrent readers while a single-row UPDATE (e.g. withdraw) is in flight.
	// busy_timeout avoids immediate "database is locked" under brief writer contention.
	if _, err := db.Exec(`PRAGMA journal_mode=WAL`); err != nil {
		_ = db.Close()
		return nil, err
	}
	if _, err := db.Exec(`PRAGMA busy_timeout=5000`); err != nil {
		_ = db.Close()
		return nil, err
	}
	if _, err := db.Exec(`PRAGMA synchronous=NORMAL`); err != nil {
		_ = db.Close()
		return nil, err
	}
	db.SetMaxOpenConns(4)
	db.SetMaxIdleConns(4)
	db.SetConnMaxLifetime(time.Hour)
	if _, err := db.Exec(`CREATE TABLE IF NOT EXISTS server_preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL)`); err != nil {
		_ = db.Close()
		return nil, err
	}
	// 配置快照与分页偏好共用服务端数据库，保证重启后协议配置不回退。
	if _, err := db.Exec(`CREATE TABLE IF NOT EXISTS config_snapshots (id INTEGER PRIMARY KEY CHECK (id = 1), payload BLOB NOT NULL, updated_at INTEGER NOT NULL)`); err != nil {
		_ = db.Close()
		return nil, err
	}
	updatesDir := filepath.Join(filepath.Dir(path), "updates")
	if err := os.MkdirAll(updatesDir, 0o700); err != nil {
		_ = db.Close()
		return nil, err
	}
	if _, err := db.Exec(`CREATE TABLE IF NOT EXISTS update_records (
		update_id TEXT PRIMARY KEY,
		component TEXT NOT NULL DEFAULT '',
		version TEXT NOT NULL DEFAULT '',
		platform TEXT NOT NULL DEFAULT '',
		client_version TEXT NOT NULL DEFAULT '',
		updater_version TEXT NOT NULL DEFAULT '',
		sha256 TEXT NOT NULL,
		package_path TEXT NOT NULL,
		metadata BLOB NOT NULL,
		status TEXT NOT NULL,
		created_at INTEGER NOT NULL,
		withdrawn_at INTEGER NOT NULL DEFAULT 0,
		seq INTEGER NOT NULL DEFAULT 0,
		status_detail TEXT NOT NULL DEFAULT '',
		force INTEGER NOT NULL DEFAULT 0
	)`); err != nil {
		_ = db.Close()
		return nil, err
	}
	if _, err := db.Exec(`CREATE TABLE IF NOT EXISTS client_update_state (
		client_id TEXT PRIMARY KEY,
		last_seq INTEGER NOT NULL DEFAULT 0,
		last_sha256 TEXT NOT NULL DEFAULT '',
		last_component TEXT NOT NULL DEFAULT '',
		last_version TEXT NOT NULL DEFAULT '',
		updated_at INTEGER NOT NULL DEFAULT 0
	)`); err != nil {
		_ = db.Close()
		return nil, err
	}
	if _, err := db.Exec(`CREATE TABLE IF NOT EXISTS update_counters (id INTEGER PRIMARY KEY CHECK (id = 1), next_seq INTEGER NOT NULL DEFAULT 1)`); err != nil {
		_ = db.Close()
		return nil, err
	}
	if _, err := db.Exec(`INSERT OR IGNORE INTO update_counters(id, next_seq) VALUES (1, 1)`); err != nil {
		_ = db.Close()
		return nil, err
	}
	// 旧开发数据库可能已经有 update_records；新增字段使用幂等迁移。
	// SQLite returns "duplicate column name" when the column already exists — treat as success.
	for _, statement := range []string{
		`ALTER TABLE update_records ADD COLUMN component TEXT NOT NULL DEFAULT ''`,
		`ALTER TABLE update_records ADD COLUMN version TEXT NOT NULL DEFAULT ''`,
		`ALTER TABLE update_records ADD COLUMN platform TEXT NOT NULL DEFAULT ''`,
		`ALTER TABLE update_records ADD COLUMN seq INTEGER NOT NULL DEFAULT 0`,
		`ALTER TABLE update_records ADD COLUMN status_detail TEXT NOT NULL DEFAULT ''`,
		`ALTER TABLE update_records ADD COLUMN force INTEGER NOT NULL DEFAULT 0`,
	} {
		if _, err := db.Exec(statement); err != nil {
			msg := strings.ToLower(err.Error())
			if strings.Contains(msg, "duplicate column") || strings.Contains(msg, "already exists") {
				continue
			}
			_ = db.Close()
			return nil, fmt.Errorf("migrate update_records: %w", err)
		}
	}
	// Ensure status_detail is queryable even if a previous migration path was skipped.
	if err := ensureUpdateRecordsStatusDetail(db); err != nil {
		_ = db.Close()
		return nil, err
	}
	// Backfill missing seq values for legacy rows so new publishes stay monotonic.
	if _, err := db.Exec(`UPDATE update_records SET seq = (
		SELECT COUNT(*) FROM update_records AS older
		WHERE older.created_at < update_records.created_at
			OR (older.created_at = update_records.created_at AND older.update_id <= update_records.update_id)
	) WHERE seq = 0`); err != nil {
		_ = db.Close()
		return nil, err
	}
	if _, err := db.Exec(`UPDATE update_counters SET next_seq = (
		SELECT COALESCE(MAX(seq), 0) + 1 FROM update_records
	) WHERE id = 1`); err != nil {
		_ = db.Close()
		return nil, err
	}
	return &serverPreferences{db: db, updatesDir: updatesDir}, nil
}

// ensureUpdateRecordsStatusDetail adds status_detail when missing (idempotent).
func ensureUpdateRecordsStatusDetail(db *sql.DB) error {
	rows, err := db.Query(`PRAGMA table_info(update_records)`)
	if err != nil {
		return err
	}
	defer rows.Close()
	hasDetail := false
	for rows.Next() {
		var cid int
		var name, ctype string
		var notnull, pk int
		var dflt sql.NullString
		if scanErr := rows.Scan(&cid, &name, &ctype, &notnull, &dflt, &pk); scanErr != nil {
			return scanErr
		}
		if strings.EqualFold(name, "status_detail") {
			hasDetail = true
			break
		}
	}
	if err := rows.Err(); err != nil {
		return err
	}
	if hasDetail {
		return nil
	}
	if _, err := db.Exec(`ALTER TABLE update_records ADD COLUMN status_detail TEXT NOT NULL DEFAULT ''`); err != nil {
		msg := strings.ToLower(err.Error())
		if strings.Contains(msg, "duplicate column") || strings.Contains(msg, "already exists") {
			return nil
		}
		return err
	}
	return nil
}

type updateRecord struct {
	UpdateID       string   `json:"update_id"`
	Seq            uint64   `json:"seq"`
	Component      string   `json:"component,omitempty"`  // compact primary: client|updater|bundle
	Components     []string `json:"components,omitempty"` // authoritative list, e.g. ["updater","client"]
	Version        string   `json:"version,omitempty"`
	Platform       string   `json:"platform,omitempty"`
	ClientVersion  string   `json:"client_version,omitempty"`
	UpdaterVersion string   `json:"updater_version,omitempty"`
	SHA256         string   `json:"sha256"`
	Status         string   `json:"status"`                  // verifying | published | failed | withdrawn
	StatusDetail   string   `json:"status_detail,omitempty"` // human-readable failure reason
	Force          bool     `json:"force,omitempty"`         // mandatory for all approved clients
	CreatedAt      int64    `json:"created_at"`
}

func (p *serverPreferences) updatePackageWritePath(sha256Hex string) (string, error) {
	if p == nil {
		return "", errors.New("update persistence unavailable")
	}
	if len(sha256Hex) != 64 {
		return "", errors.New("invalid update SHA-256")
	}
	root, err := filepath.Abs(p.updatesDir)
	if err != nil {
		return "", err
	}
	clean, err := filepath.Abs(filepath.Join(p.updatesDir, strings.ToLower(sha256Hex)+".tar.zst"))
	if err != nil || filepath.Dir(clean) != root {
		return "", errors.New("invalid update path")
	}
	return clean, nil
}

func (p *serverPreferences) updatePackagePath(sha256Hex string) (string, error) {
	if p == nil {
		return "", errors.New("update persistence unavailable")
	}
	if len(sha256Hex) != 64 {
		return "", errors.New("invalid update SHA-256")
	}
	root, err := filepath.Abs(p.updatesDir)
	if err != nil {
		return "", err
	}
	// Canonical on-disk name is <sha256>.tar.zst. Legacy <sha256>.zst remains readable.
	candidates := []string{
		filepath.Join(p.updatesDir, strings.ToLower(sha256Hex)+".tar.zst"),
		filepath.Join(p.updatesDir, strings.ToLower(sha256Hex)+".zst"),
	}
	for _, path := range candidates {
		clean, absErr := filepath.Abs(path)
		if absErr != nil || filepath.Dir(clean) != root {
			continue
		}
		if _, statErr := os.Stat(clean); statErr == nil {
			return clean, nil
		}
	}
	return p.updatePackageWritePath(sha256Hex)
}

// saveUpdateFile moves an already-written package file into the durable updates directory.
// The source path is consumed (renamed) on success so large packages are never held in RAM.
// When sourcePath is empty, only the DB row is written (metadata-only / tests).
func (p *serverPreferences) saveUpdateFile(record updateRecord, metadata []byte, sourcePath string) error {
	if p == nil || p.db == nil {
		return errors.New("update persistence unavailable")
	}
	path, err := p.updatePackageWritePath(record.SHA256)
	if err != nil {
		return err
	}
	if sourcePath != "" && sourcePath != path {
		if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
			return err
		}
		if err := os.Rename(sourcePath, path); err != nil {
			// Cross-device rename fallback: stream copy then remove source.
			if copyErr := copyFileStream(sourcePath, path); copyErr != nil {
				return copyErr
			}
			_ = os.Remove(sourcePath)
		}
	} else if sourcePath == "" {
		path = ""
	}
	if record.Seq == 0 {
		seq, allocErr := p.allocateUpdateSeq()
		if allocErr != nil {
			return allocErr
		}
		record.Seq = seq
	}
	forceFlag := 0
	if record.Force {
		forceFlag = 1
	}
	// Single-row upsert by primary key; no process-wide mutex (WAL handles concurrency).
	_, err = p.db.Exec(
		`INSERT INTO update_records(update_id, component, version, platform, client_version, updater_version, sha256, package_path, metadata, status, created_at, withdrawn_at, seq, status_detail, force)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
		 ON CONFLICT(update_id) DO UPDATE SET
			component=excluded.component,
			version=excluded.version,
			platform=excluded.platform,
			client_version=excluded.client_version,
			updater_version=excluded.updater_version,
			sha256=excluded.sha256,
			package_path=excluded.package_path,
			metadata=excluded.metadata,
			status=excluded.status,
			created_at=excluded.created_at,
			seq=excluded.seq,
			status_detail=excluded.status_detail,
			force=excluded.force`,
		record.UpdateID, record.Component, record.Version, record.Platform, record.ClientVersion, record.UpdaterVersion,
		record.SHA256, path, metadata, record.Status, record.CreatedAt, int64(record.Seq), record.StatusDetail, forceFlag,
	)
	return err
}

func (p *serverPreferences) setUpdateStatus(updateID, status, detail string) error {
	if p == nil || p.db == nil {
		return errors.New("update persistence unavailable")
	}
	// Row-level only: other update_id rows and unrelated tables stay usable.
	result, err := p.db.Exec(
		`UPDATE update_records SET status=?, status_detail=? WHERE update_id=? AND status!='withdrawn'`,
		status, detail, updateID,
	)
	if err != nil {
		return err
	}
	n, _ := result.RowsAffected()
	if n == 0 {
		return errors.New("update not found or already withdrawn")
	}
	return nil
}

func (p *serverPreferences) getUpdateRecord(updateID string) (updateRecord, []byte, string, error) {
	if p == nil || p.db == nil {
		return updateRecord{}, nil, "", errors.New("update persistence unavailable")
	}
	var item updateRecord
	var seq int64
	var metadata []byte
	var path string
	var forceFlag int
	err := p.db.QueryRow(`SELECT update_id, component, version, platform, client_version, updater_version, sha256, status, created_at, seq, COALESCE(status_detail,''), COALESCE(force,0), metadata, package_path
		FROM update_records WHERE update_id = ?`, updateID).
		Scan(&item.UpdateID, &item.Component, &item.Version, &item.Platform, &item.ClientVersion, &item.UpdaterVersion, &item.SHA256, &item.Status, &item.CreatedAt, &seq, &item.StatusDetail, &forceFlag, &metadata, &path)
	if err != nil {
		// Older DBs without force column.
		err = p.db.QueryRow(`SELECT update_id, component, version, platform, client_version, updater_version, sha256, status, created_at, seq, COALESCE(status_detail,''), metadata, package_path
			FROM update_records WHERE update_id = ?`, updateID).
			Scan(&item.UpdateID, &item.Component, &item.Version, &item.Platform, &item.ClientVersion, &item.UpdaterVersion, &item.SHA256, &item.Status, &item.CreatedAt, &seq, &item.StatusDetail, &metadata, &path)
		if err != nil {
			return updateRecord{}, nil, "", err
		}
	}
	item.Seq = uint64(seq)
	item.Force = forceFlag != 0
	if len(metadata) > 0 {
		var meta publishUpdateRequest
		if json.Unmarshal(metadata, &meta) == nil {
			if comps, primary, normErr := normalizeUpdateComponents(meta.Components, meta.Component); normErr == nil {
				item.Components = comps
				if item.Component == "" {
					item.Component = primary
				}
			}
		}
	}
	return item, metadata, path, nil
}

// allocateUpdateSeq returns the next monotonic publish sequence.
// Callers that already hold a Seq on the record should keep it and skip allocation.

func (p *serverPreferences) allocateUpdateSeq() (uint64, error) {
	if p == nil || p.db == nil {
		return uint64(time.Now().UnixNano()), nil
	}
	// Only the counter RMW needs process serialization; withdraw/list stay unlocked.
	p.seqMu.Lock()
	defer p.seqMu.Unlock()
	var next int64
	if err := p.db.QueryRow(`SELECT next_seq FROM update_counters WHERE id = 1`).Scan(&next); err != nil {
		return 0, err
	}
	if next < 1 {
		next = 1
	}
	if _, err := p.db.Exec(`UPDATE update_counters SET next_seq = ? WHERE id = 1`, next+1); err != nil {
		return 0, err
	}
	return uint64(next), nil
}

func (p *serverPreferences) markClientUpdateOffered(clientID string, record updateRecord) error {
	if p == nil || p.db == nil || clientID == "" || record.Seq == 0 {
		return nil
	}
	// Per-client row upsert (PRIMARY KEY client_id); does not lock update_records.
	_, err := p.db.Exec(`INSERT INTO client_update_state(client_id, last_seq, last_sha256, last_component, last_version, updated_at)
		VALUES (?, ?, ?, ?, ?, ?)
		ON CONFLICT(client_id) DO UPDATE SET
			last_seq = excluded.last_seq,
			last_sha256 = excluded.last_sha256,
			last_component = excluded.last_component,
			last_version = excluded.last_version,
			updated_at = excluded.updated_at
		WHERE excluded.last_seq >= client_update_state.last_seq`,
		clientID, int64(record.Seq), record.SHA256, record.Component, record.Version, time.Now().UnixMilli())
	return err
}

func (p *serverPreferences) clientUpdateSeq(clientID string) uint64 {
	if p == nil || p.db == nil || clientID == "" {
		return 0
	}
	var seq int64
	if err := p.db.QueryRow(`SELECT last_seq FROM client_update_state WHERE client_id = ?`, clientID).Scan(&seq); err != nil {
		return 0
	}
	if seq < 0 {
		return 0
	}
	return uint64(seq)
}

func (p *serverPreferences) packagePathBySHA(sha256Hex string) (string, updateRecord, error) {
	if p == nil || p.db == nil {
		return "", updateRecord{}, errors.New("update persistence unavailable")
	}
	sha256Hex = strings.ToLower(strings.TrimSpace(sha256Hex))
	if len(sha256Hex) != 64 {
		return "", updateRecord{}, errors.New("invalid update SHA-256")
	}
	var item updateRecord
	var path string
	var seq int64
	// Only fully verified packages are downloadable.
	err := p.db.QueryRow(`SELECT update_id, component, version, platform, client_version, updater_version, sha256, status, created_at, package_path, seq, COALESCE(status_detail,'')
		FROM update_records WHERE sha256 = ? AND status = 'published' ORDER BY seq DESC LIMIT 1`, sha256Hex).
		Scan(&item.UpdateID, &item.Component, &item.Version, &item.Platform, &item.ClientVersion, &item.UpdaterVersion, &item.SHA256, &item.Status, &item.CreatedAt, &path, &seq, &item.StatusDetail)
	if err != nil {
		return "", updateRecord{}, err
	}
	item.Seq = uint64(seq)
	return path, item, nil
}

func (p *serverPreferences) saveUpdate(record updateRecord, metadata []byte, packageBytes []byte) error {
	if p == nil || p.db == nil {
		return errors.New("update persistence unavailable")
	}
	if len(packageBytes) == 0 {
		return p.saveUpdateFile(record, metadata, "")
	}
	if err := os.MkdirAll(p.updatesDir, 0o700); err != nil {
		return err
	}
	temporary, err := os.CreateTemp(p.updatesDir, ".update-*")
	if err != nil {
		return err
	}
	name := temporary.Name()
	defer func() { _ = temporary.Close(); _ = os.Remove(name) }()
	if _, err = temporary.Write(packageBytes); err != nil {
		return err
	}
	if err = temporary.Sync(); err != nil {
		return err
	}
	if err = temporary.Close(); err != nil {
		return err
	}
	return p.saveUpdateFile(record, metadata, name)
}

func copyFileStream(source, destination string) error {
	in, err := os.Open(source)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(destination, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	defer func() { _ = out.Close() }()
	if _, err := io.Copy(out, in); err != nil {
		return err
	}
	return out.Sync()
}

func (p *serverPreferences) updateRecords() ([]updateRecord, error) {
	if p == nil || p.db == nil {
		return nil, nil
	}
	// Read-only list; concurrent withdraw of one row must not block this under WAL.
	// Prefer the full projection; fall back if an older DB never got status_detail/force.
	rows, err := p.db.Query(`SELECT update_id, component, version, platform, client_version, updater_version, sha256, status, created_at, seq, COALESCE(status_detail,''), COALESCE(force,0), metadata FROM update_records ORDER BY seq DESC, created_at DESC`)
	includeDetail := true
	includeForce := true
	if err != nil {
		msg := strings.ToLower(err.Error())
		if strings.Contains(msg, "force") {
			rows, err = p.db.Query(`SELECT update_id, component, version, platform, client_version, updater_version, sha256, status, created_at, seq, COALESCE(status_detail,''), metadata FROM update_records ORDER BY seq DESC, created_at DESC`)
			includeForce = false
		} else if strings.Contains(msg, "status_detail") {
			rows, err = p.db.Query(`SELECT update_id, component, version, platform, client_version, updater_version, sha256, status, created_at, seq, metadata FROM update_records ORDER BY seq DESC, created_at DESC`)
			includeDetail = false
			includeForce = false
		}
		if err != nil {
			return nil, err
		}
	}
	defer rows.Close()
	items := []updateRecord{}
	for rows.Next() {
		var item updateRecord
		var seq int64
		var metadata []byte
		var forceFlag int
		var scanErr error
		if includeForce {
			scanErr = rows.Scan(&item.UpdateID, &item.Component, &item.Version, &item.Platform, &item.ClientVersion, &item.UpdaterVersion, &item.SHA256, &item.Status, &item.CreatedAt, &seq, &item.StatusDetail, &forceFlag, &metadata)
		} else if includeDetail {
			scanErr = rows.Scan(&item.UpdateID, &item.Component, &item.Version, &item.Platform, &item.ClientVersion, &item.UpdaterVersion, &item.SHA256, &item.Status, &item.CreatedAt, &seq, &item.StatusDetail, &metadata)
		} else {
			scanErr = rows.Scan(&item.UpdateID, &item.Component, &item.Version, &item.Platform, &item.ClientVersion, &item.UpdaterVersion, &item.SHA256, &item.Status, &item.CreatedAt, &seq, &metadata)
		}
		if scanErr != nil {
			return nil, scanErr
		}
		item.Seq = uint64(seq)
		item.Force = forceFlag != 0
		if len(metadata) > 0 {
			var meta publishUpdateRequest
			if json.Unmarshal(metadata, &meta) == nil {
				if comps, primary, normErr := normalizeUpdateComponents(meta.Components, meta.Component); normErr == nil {
					item.Components = comps
					if item.Component == "" {
						item.Component = primary
					}
				}
			}
		}
		if len(item.Components) == 0 {
			if comps, primary, normErr := normalizeUpdateComponents(nil, item.Component); normErr == nil {
				item.Components = comps
				item.Component = primary
			}
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

func (p *serverPreferences) withdrawUpdate(id string) error {
	if p == nil || p.db == nil {
		return errors.New("update persistence unavailable")
	}
	id = strings.TrimSpace(id)
	if id == "" {
		return errors.New("update id is required")
	}
	// Touch only this primary-key row. Do not take seqMu or any table-wide lock.
	result, err := p.db.Exec(
		`UPDATE update_records SET status='withdrawn', withdrawn_at=? WHERE update_id=? AND status!='withdrawn'`,
		time.Now().UnixMilli(),
		id,
	)
	if err != nil {
		return err
	}
	n, _ := result.RowsAffected()
	if n == 0 {
		return errors.New("update not found or already withdrawn")
	}
	return nil
}

func (p *serverPreferences) pageSize() int {
	if p == nil || p.db == nil {
		return 25
	}
	var value int
	if err := p.db.QueryRow(`SELECT value FROM server_preferences WHERE key = 'page_size'`).Scan(&value); err != nil || !validPageSize(value) {
		return 25
	}
	return value
}

func (p *serverPreferences) setPageSize(value int) error {
	if !validPageSize(value) {
		return errors.New("page size must be one of 10, 15, 20, 25, 30, 50, 100 or 200")
	}
	if p == nil || p.db == nil {
		return nil
	}
	_, err := p.db.Exec(`INSERT INTO server_preferences(key, value) VALUES ('page_size', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value`, value)
	return err
}

// loadConfigSnapshot 读取持久化配置；首次启动时创建默认快照。
func (p *serverPreferences) loadConfigSnapshot(now time.Time) (config.Snapshot, error) {
	if p == nil || p.db == nil {
		return config.NewSnapshot(now)
	}
	var payload []byte
	err := p.db.QueryRow(`SELECT payload FROM config_snapshots WHERE id = 1`).Scan(&payload)
	if errors.Is(err, sql.ErrNoRows) {
		return config.NewSnapshot(now)
	}
	if err != nil {
		return config.Snapshot{}, err
	}
	var snapshot config.Snapshot
	if err := json.Unmarshal(payload, &snapshot); err != nil {
		return config.Snapshot{}, err
	}
	if snapshot.ID == "" || snapshot.IssuedAt <= 0 {
		return config.Snapshot{}, errors.New("persisted config snapshot is incomplete")
	}
	return snapshot, nil
}

// saveConfigSnapshot 将已发布配置原子写入 SQLite。
func (p *serverPreferences) saveConfigSnapshot(snapshot config.Snapshot) error {
	if p == nil || p.db == nil {
		return nil
	}
	payload, err := json.Marshal(snapshot)
	if err != nil {
		return err
	}
	_, err = p.db.Exec(`INSERT INTO config_snapshots(id, payload, updated_at) VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at`, payload, time.Now().UnixMilli())
	return err
}

func validPageSize(value int) bool {
	switch value {
	case 10, 15, 20, 25, 30, 50, 100, 200:
		return true
	default:
		return false
	}
}

type eventBroker struct {
	mu      sync.RWMutex
	clients map[chan string]struct{}
}

func newEventBroker() *eventBroker { return &eventBroker{clients: make(map[chan string]struct{})} }

func (b *eventBroker) subscribe() (chan string, func()) {
	channel := make(chan string, 8)
	b.mu.Lock()
	b.clients[channel] = struct{}{}
	b.mu.Unlock()
	return channel, func() {
		b.mu.Lock()
		if _, ok := b.clients[channel]; ok {
			delete(b.clients, channel)
			close(channel)
		}
		b.mu.Unlock()
	}
}

func (b *eventBroker) publish(event string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	for channel := range b.clients {
		select {
		case channel <- event:
		default:
			// A slow browser may not be able to consume events quickly enough.
			// Preserve correctness by replacing the backlog with one full-refresh
			// notification instead of silently losing an invalidation.
			for len(channel) > 0 {
				<-channel
			}
			channel <- "all"
		}
	}
}

var managementEvents = newEventBroker()

// Keep the in-memory log ring small on low-RAM boards (class sticks ~256–512 MiB).
const maxInMemoryLogLines = 1000

// logBuffer 保存最近的 JSONL 日志，管理 API 只读这份有界内存副本，避免每次分页都扫描磁盘文件。
type logBuffer struct {
	mu    sync.RWMutex
	max   int
	lines []string
}

// clientLogBuffer stores only logs uploaded by authenticated client sessions.
// It is deliberately separate from the server process log buffer.
type clientLogBuffer struct {
	mu         sync.RWMutex
	max        int
	maxClients int
	entries    map[string][]string
	notifiedAt map[string]time.Time
}

func newClientLogBuffer(max int) *clientLogBuffer {
	if max < 1 {
		max = 500
	}
	return &clientLogBuffer{max: max, maxClients: 1000, entries: make(map[string][]string), notifiedAt: make(map[string]time.Time)}
}

func (b *clientLogBuffer) append(clientID, entry string) bool {
	if b == nil || clientID == "" || len(entry) == 0 || len(entry) > 16*1024 || !json.Valid([]byte(entry)) {
		return false
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if _, exists := b.entries[clientID]; !exists && len(b.entries) >= b.maxClients {
		for oldest := range b.entries {
			delete(b.entries, oldest)
			delete(b.notifiedAt, oldest)
			break
		}
	}
	entries := append(b.entries[clientID], entry)
	if len(entries) > b.max {
		entries = append([]string(nil), entries[len(entries)-b.max:]...)
	}
	b.entries[clientID] = entries
	now := time.Now()
	if now.Sub(b.notifiedAt[clientID]) >= time.Second {
		b.notifiedAt[clientID] = now
		return true
	}
	return false
}

func (b *clientLogBuffer) snapshot(clientID string) string {
	if b == nil {
		return ""
	}
	b.mu.RLock()
	defer b.mu.RUnlock()
	return strings.Join(append([]string(nil), b.entries[clientID]...), "\n")
}

// readClientLogPage returns client logs newest-first and applies pagination on the server.
func readClientLogPage(content string, page, pageSize int) (string, int) {
	lines := strings.Split(content, "\n")
	type timedLine struct {
		line      string
		timestamp time.Time
		index     int
	}
	entries := make([]timedLine, 0, len(lines))
	for index, line := range lines {
		if strings.TrimSpace(line) == "" {
			continue
		}
		var value struct {
			Time string `json:"time"`
		}
		var timestamp time.Time
		if json.Unmarshal([]byte(line), &value) == nil {
			timestamp, _ = time.Parse(time.RFC3339Nano, value.Time)
		}
		entries = append(entries, timedLine{line: line, timestamp: timestamp, index: index})
	}
	sort.SliceStable(entries, func(i, j int) bool {
		if entries[i].timestamp.Equal(entries[j].timestamp) {
			return entries[i].index > entries[j].index
		}
		return entries[i].timestamp.After(entries[j].timestamp)
	})
	total := len(entries)
	if page < 1 {
		page = 1
	}
	start := (page - 1) * pageSize
	if start >= total {
		return "", total
	}
	end := start + pageSize
	if end > total {
		end = total
	}
	selected := make([]string, 0, end-start)
	for _, entry := range entries[start:end] {
		selected = append(selected, entry.line)
	}
	return strings.Join(selected, "\n"), total
}

func newLogBuffer(max int) *logBuffer {
	if max < 1 {
		max = maxInMemoryLogLines
	}
	return &logBuffer{max: max}
}

func (b *logBuffer) append(data []byte) {
	if b == nil || len(data) == 0 {
		return
	}
	lines := strings.Split(strings.TrimRight(string(data), "\r\n"), "\n")
	b.mu.Lock()
	defer b.mu.Unlock()
	for _, line := range lines {
		if strings.TrimSpace(line) == "" {
			continue
		}
		b.lines = append(b.lines, line)
	}
	if len(b.lines) > b.max {
		b.lines = append([]string(nil), b.lines[len(b.lines)-b.max:]...)
	}
}

func (b *logBuffer) snapshot() []string {
	if b == nil {
		return nil
	}
	b.mu.RLock()
	defer b.mu.RUnlock()
	return append([]string(nil), b.lines...)
}

type logEventWriter struct {
	writer io.Writer
	buffer *logBuffer
}

func (w logEventWriter) Write(p []byte) (int, error) {
	n, err := w.writer.Write(p)
	if n > 0 {
		w.buffer.append(p[:n])
	}
	if n > 0 && !bytes.Contains(p, []byte(`"path":"/api/v1/logs"`)) && !bytes.Contains(p, []byte(`"path":"/api/v1/events"`)) {
		managementEvents.publish("logs")
	}
	return n, err
}

func main() {
	httpAddr := flag.String("http-addr", "127.0.0.1:39003", "management API + admin UI listen address (LAN clients download updates over TLS, not HTTP)")
	udpAddr := flag.String("udp-addr", ":39001", "UDP discovery listen address")
	tlsAddr := flag.String("tls-addr", ":39002", "TLS client listen address (business + update download)")
	identityDir := flag.String("identity-dir", "", "directory for the persistent server identity (default: data beside executable)")
	flag.Parse()
	identityPath := *identityDir
	if identityPath == "" {
		// executable, executableErr := os.Executable()
		// if executableErr == nil && !strings.HasPrefix(filepath.Clean(executable), filepath.Clean(os.TempDir())+string(os.PathSeparator)) {
		// 	identityPath = filepath.Join(filepath.Dir(executable), "data")
		// } else {
		// 	// `go run` places the temporary executable under os.TempDir(); use
		// 	// the project root so running from a subdirectory still reuses it.
		// }
		identityPath = "data"
	}
	if err := os.MkdirAll(identityPath, 0o700); err != nil {
		logError := slog.New(slog.NewTextHandler(os.Stderr, nil))
		logError.Error("create identity directory", "error", err)
		return
	}

	logFile, err := os.OpenFile(filepath.Join(identityPath, "server.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		logger := slog.New(slog.NewTextHandler(os.Stderr, nil))
		logger.Error("open server log", "error", err)
		return
	}
	defer logFile.Close()
	serverLogBuffer := newLogBuffer(maxInMemoryLogLines)
	clientLogs := newClientLogBuffer(500)
	logger := slog.New(slog.NewJSONHandler(io.MultiWriter(os.Stdout, logEventWriter{writer: logFile, buffer: serverLogBuffer}), &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)
	serverIdentity, err := identity.LoadOrCreate(identityPath, "lovemilk-class-broadcaster", time.Now())
	if err != nil {
		logger.Error("create server identity", "error", err)
		return
	}
	serverPreferences, err := openServerPreferences(filepath.Join(identityPath, "server.db"))
	if err != nil {
		logger.Error("load server preferences", "error", err)
		return
	}
	persistedSnapshot, err := serverPreferences.loadConfigSnapshot(time.Now())
	if err != nil {
		logger.Error("load config snapshot", "error", err)
		return
	}
	configManager, err := config.NewManagerFromSnapshot(persistedSnapshot)
	if err != nil {
		logger.Error("create config manager", "error", err)
		return
	}
	configManager.SetPersistence(serverPreferences.saveConfigSnapshot)
	if err := serverPreferences.saveConfigSnapshot(persistedSnapshot); err != nil {
		logger.Error("persist config snapshot", "error", err)
		return
	}
	messageStore, err := broadcast.NewPersistentStore(filepath.Join(identityPath, "server.db"))
	if err != nil {
		logger.Error("load persistent message store", "error", err)
		return
	}
	deviceRegistry, err := session.NewPersistentRegistry(filepath.Join(identityPath, "server.db"))
	if err != nil {
		logger.Error("load device registry", "error", err)
		return
	}
	applyListenerPolicy := func(snapshot config.Snapshot) {
		deviceRegistry.SetListenerPolicy(session.ListenerPolicy{
			ProbeInterval: snapshot.ListenerProbeInterval,
			ProbeDuration: snapshot.ListenerProbeDuration,
			ProbeReset:    snapshot.ListenerProbeReset,
			LossThreshold: int(snapshot.ListenerLossThreshold),
			IdleTimeout:   snapshot.ListenerIdleTimeout,
		})
	}
	applyListenerPolicy(persistedSnapshot)
	logger.Info("persistent server state loaded", "data_dir", identityPath, "database", filepath.Join(identityPath, "server.db"), "devices", len(deviceRegistry.List()), "config_id", configManager.Current().ID)
	sessionHub := session.NewHub()
	shutdownRequest := make(chan struct{}, 1)
	controlledShutdown := make(chan struct{}, 1)
	requestShutdown := func() {
		select {
		case shutdownRequest <- struct{}{}:
			controlledShutdown <- struct{}{}
		default:
		}
	}

	server := &http.Server{
		Addr: *httpAddr,
		Handler: newRouterWithDependencies(
			messageStore,
			deviceRegistry,
			sessionHub,
			configManager,
			serverPreferences,
			requestShutdown,
			serverLogBuffer,
			clientLogs,
			serverIdentity,
			portOf(*tlsAddr),
			portOf(*httpAddr),
			portOf(*udpAddr),
		),
		ReadHeaderTimeout: 5 * time.Second,
		// Large multipart update uploads need more than 15s.
		ReadTimeout: 10 * time.Minute,
		// The management API includes a long-lived SSE endpoint; a server-wide
		// write timeout would silently terminate every event stream after 15s.
		WriteTimeout: 0,
		IdleTimeout:  120 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	// Propagate process shutdown to all management API requests. In particular,
	// the long-lived SSE endpoint otherwise remains an active request and makes
	// http.Server.Shutdown wait until its timeout expires.
	server.BaseContext = func(net.Listener) context.Context { return ctx }
	go func() {
		select {
		case <-shutdownRequest:
			stop()
		case <-ctx.Done():
		}
	}()

	serverErr := make(chan error, 1)
	udpSocket, err := net.ResolveUDPAddr("udp4", *udpAddr)
	if err != nil {
		logger.Error("resolve UDP address", "error", err)
		return
	}
	udpConn, err := net.ListenUDP("udp4", udpSocket)
	if err != nil {
		logger.Error("listen UDP discovery", "addr", *udpAddr, "error", err)
		return
	}
	go func() {
		defer udpConn.Close()
		logger.Info("UDP discovery listening", "addr", udpConn.LocalAddr())
		if err := (discovery.Responder{Identity: serverIdentity, ServiceName: "lovemilk-class-broadcaster", TCPPort: portOf(*tlsAddr), HTTPPort: portOf(*httpAddr)}).Serve(ctx, udpConn); err != nil && !errors.Is(err, net.ErrClosed) {
			serverErr <- err
		}
	}()

	tlsCertificate, err := serverIdentity.TLSCertificate()
	if err != nil {
		logger.Error("create TLS certificate", "error", err)
		return
	}
	tlsListener, err := net.Listen("tcp", *tlsAddr)
	if err != nil {
		logger.Error("listen TLS", "addr", *tlsAddr, "error", err)
		return
	}
	go func() {
		defer tlsListener.Close()
		listener := tls.NewListener(tlsListener, newTLSConfig(tlsCertificate))
		logger.Info("TLS client listening", "addr", tlsListener.Addr())
		for {
			conn, acceptErr := listener.Accept()
			if acceptErr != nil {
				select {
				case <-ctx.Done():
					return
				default:
				}
				serverErr <- acceptErr
				return
			}
			go serveTLSConnection(ctx, conn, serverIdentity, configManager, deviceRegistry, sessionHub, messageStore, clientLogs, serverPreferences)
		}
	}()
	go dispatchMessages(ctx, messageStore, sessionHub, configManager, deviceRegistry)
	go probeClientListeners(ctx, serverIdentity, deviceRegistry, configManager)
	go runRetentionCleanup(ctx, messageStore, deviceRegistry)
	go runSessionEndWatcher(ctx, deviceRegistry, sessionHub)

	go func() {
		logger.Info("management API listening", "addr", *httpAddr, "framework", "gin")
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			serverErr <- err
		}
	}()

	select {
	case <-ctx.Done():
		// Only the management "关闭服务端" API sends SERVER_SHUTDOWN.
		// Ctrl+C / SIGTERM must not advertise a controlled maintenance shutdown;
		// they only need a fast management-API teardown (SSE connections).
		controlled := false
		select {
		case <-controlledShutdown:
			controlled = true
		default:
		}
		if controlled {
			shutdownPacket := protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.ServerShutdown, 0, 0, nil)
			for _, clientID := range sessionHub.OnlineIDs() {
				_ = sessionHub.Send(clientID, shutdownPacket)
			}
			time.Sleep(200 * time.Millisecond)
		}
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		shutdownErr := server.Shutdown(shutdownCtx)
		cancel()
		if shutdownErr != nil {
			// Long-lived SSE requests are active handlers by design. Shutdown
			// waits for them, so force-close the remaining HTTP connections after
			// the grace period instead of treating a normal Ctrl+C as a failure.
			logger.Warn("management API graceful shutdown timed out; closing active connections", "error", shutdownErr)
			if closeErr := server.Close(); closeErr != nil && !errors.Is(closeErr, http.ErrServerClosed) {
				logger.Error("management API force close failed", "error", closeErr)
			}
		}
	case err := <-serverErr:
		if err != nil {
			logger.Error("management API stopped", "error", err)
			os.Exit(1)
		}
	}
}

const historyRetention = 185 * 24 * time.Hour

type retentionCleanupResult struct {
	MaxAgeDays      int   `json:"max_age_days"`
	MessagesRemoved int   `json:"messages_removed"`
	DevicesRemoved  int   `json:"devices_removed"`
	RanAt           int64 `json:"ran_at"`
}

func purgeExpiredHistory(store *broadcast.Store, registry *session.Registry, now time.Time) retentionCleanupResult {
	result := retentionCleanupResult{MaxAgeDays: 185, RanAt: now.UnixMilli()}
	if store != nil {
		result.MessagesRemoved = store.PurgeOlderThan(now, historyRetention)
	}
	if registry != nil {
		result.DevicesRemoved = registry.PurgeStaleDevices(now, historyRetention)
	}
	if result.MessagesRemoved > 0 {
		managementEvents.publish("messages")
	}
	if result.DevicesRemoved > 0 {
		managementEvents.publish("devices")
	}
	return result
}

// runSessionEndWatcher promotes unexplained drops to 意外终止 after 30s without reconnect.
func runSessionEndWatcher(ctx context.Context, registry *session.Registry, hub *session.Hub) {
	if registry == nil || hub == nil {
		return
	}
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			onlineSet := make(map[string]struct{})
			for _, id := range hub.OnlineIDs() {
				onlineSet[id] = struct{}{}
			}
			if registry.FinalizeUnexpectedDisconnects(onlineSet, now) > 0 {
				managementEvents.publish("devices")
			}
		}
	}
}

// runRetentionCleanup periodically drops message and client records older than 185 days.
// This covers finished message history and idle device registry entries (any status).
func runRetentionCleanup(ctx context.Context, store *broadcast.Store, registry *session.Registry) {
	runOnce := func(now time.Time) {
		result := purgeExpiredHistory(store, registry, now)
		if result.MessagesRemoved > 0 || result.DevicesRemoved > 0 {
			slog.Info("retention cleanup completed",
				"max_age_days", result.MaxAgeDays,
				"messages_removed", result.MessagesRemoved,
				"devices_removed", result.DevicesRemoved)
		}
	}
	runOnce(time.Now())
	ticker := time.NewTicker(6 * time.Hour)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			runOnce(now)
		}
	}
}

// probeClientListeners 每分钟探测声明监听能力的客户端，统计 12 小时试用窗口内的丢包和延迟。
// 消息主链路仍保留 TLS 长连接，只有试用窗口结束且丢包率不高于 10% 才标记为监听模式。
func probeClientListeners(ctx context.Context, serverIdentity identity.Identity, registry *session.Registry, configManager *config.Manager) {
	interval := config.DefaultListenerProbeInterval
	if configManager != nil && configManager.Current().ListenerProbeInterval > 0 {
		interval = configManager.Current().ListenerProbeInterval
	}
	ticker := time.NewTimer(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			for _, device := range registry.List() {
				if device.Status != "approved" || device.ListenerPort <= 0 || device.ListenerAddress == "" {
					continue
				}
				if device.RequestedMode == "auto" && device.ProbeUntil == 0 {
					continue
				}
				latency, received := probeClientListener(serverIdentity, device.ClientID, device.ListenerAddress, device.ListenerPort, registry.ListenerPolicy().IdleTimeout)
				registry.RecordListenerProbe(device.ClientID, received, latency, now)
			}
			interval = registry.ListenerPolicy().ProbeInterval
			if interval <= 0 {
				interval = config.DefaultListenerProbeInterval
			}
			ticker.Reset(interval)
		}
	}
}

func probeClientListener(serverIdentity identity.Identity, expectedClientID, address string, port int, idleTimeout time.Duration) (time.Duration, bool) {
	started := time.Now()
	raw, err := net.DialTimeout("tcp", net.JoinHostPort(address, strconv.Itoa(port)), 3*time.Second)
	if err != nil {
		return 0, false
	}
	defer raw.Close()
	certificate, err := serverIdentity.TLSCertificate()
	if err != nil {
		return 0, false
	}
	connection := tls.Client(raw, &tls.Config{MinVersion: tls.VersionTLS13, MaxVersion: tls.VersionTLS13, Certificates: []tls.Certificate{certificate}, InsecureSkipVerify: true})
	defer connection.Close()
	if err := connection.Handshake(); err != nil {
		return 0, false
	}
	if idleTimeout <= 0 {
		idleTimeout = config.DefaultListenerIdleTimeout
	}
	_ = connection.SetDeadline(time.Now().Add(idleTimeout))
	// 监听探测使用随机 nonce 和 Ed25519 签名证明客户端确实持有对应私钥。
	var nonce [32]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return 0, false
	}
	serverPublicKey := serverIdentity.PublicKey
	signedChallenge := listenerProbeSigningBytes(nonce[:], serverPublicKey)
	challenge := append(append([]byte(nil), nonce[:]...), serverPublicKey...)
	challenge = append(challenge, ed25519.Sign(serverIdentity.PrivateKey, signedChallenge)...)
	if err := protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.Ping, 1, 0, challenge).WritePacket(connection); err != nil {
		return 0, false
	}
	packet, err := protocol.ReadPacket(connection)
	if err != nil || packet.Type != protocol.Pong || packet.Seq != 1 || len(packet.Payload) != ed25519.PublicKeySize+ed25519.SignatureSize {
		return 0, false
	}
	clientPublicKey := ed25519.PublicKey(packet.Payload[:ed25519.PublicKeySize])
	clientID, idErr := session.ClientID(clientPublicKey)
	challengeSignature := packet.Payload[ed25519.PublicKeySize:]
	if idErr != nil || !strings.EqualFold(clientID, expectedClientID) || !ed25519.Verify(clientPublicKey, listenerProbeSigningBytes(nonce[:], serverPublicKey), challengeSignature) {
		return 0, false
	}
	return time.Since(started), true
}

func listenerProbeSigningBytes(nonce, serverPublicKey []byte) []byte {
	message := append([]byte("MKCB-listener-probe-v1\x00"), nonce...)
	return append(message, serverPublicKey...)
}

func newTLSConfig(certificate tls.Certificate) *tls.Config {
	return &tls.Config{
		MinVersion:   tls.VersionTLS13,
		Certificates: []tls.Certificate{certificate},
		ClientAuth:   tls.RequireAnyClientCert,
	}
}

func serveTLSConnection(ctx context.Context, raw net.Conn, serverIdentity identity.Identity, configManager *config.Manager, registry *session.Registry, hub *session.Hub, store *broadcast.Store, clientLogs *clientLogBuffer, preferences *serverPreferences) {
	defer raw.Close()
	remoteAddr := ""
	if raw != nil && raw.RemoteAddr() != nil {
		remoteAddr = raw.RemoteAddr().String()
	}
	clientIP, clientPort := splitRemoteAddr(remoteAddr)
	conn, ok := raw.(*tls.Conn)
	if !ok {
		return
	}
	if err := conn.HandshakeContext(ctx); err != nil {
		slog.Warn("TLS handshake failed", "error", err, "client_ip", clientIP, "client_port", clientPort, "remote_addr", remoteAddr)
		return
	}
	state := conn.ConnectionState()
	if len(state.PeerCertificates) == 0 {
		return
	}
	clientEdKey, ok := state.PeerCertificates[0].PublicKey.(ed25519.PublicKey)
	if !ok {
		slog.Warn("client certificate is not Ed25519", "client_ip", clientIP, "client_port", clientPort, "remote_addr", remoteAddr)
		return
	}
	clientID, clientIDErr := session.ClientID(clientEdKey)
	if clientIDErr != nil {
		slog.Warn("calculate client fingerprint failed", "error", clientIDErr, "client_ip", clientIP, "client_port", clientPort, "remote_addr", remoteAddr)
		return
	}
	if !registry.IsApproved(clientEdKey) {
		_, observeErr := registry.ObservePending(clientEdKey, time.Now())
		if observeErr != nil {
			slog.Warn("record pending client failed", "client_id", clientID, "error", observeErr, "client_ip", clientIP, "client_port", clientPort, "remote_addr", remoteAddr)
		}
		managementEvents.publish("devices")
		slog.Warn("client rejected", "client_id", clientID, "reason", "client_not_approved", "client_ip", clientIP, "client_port", clientPort, "remote_addr", remoteAddr)
		payload, _ := protocol.MarshalBSON(map[string]any{"code": "client_not_approved", "message": "client public key is pending approval", "client_id": clientID, "retryable": true})
		_ = protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.Error, 0, 0, payload).WritePacket(conn)
		return
	}
	packet, err := protocol.ReadPacket(conn)
	if err != nil || packet.Type != protocol.ConnectReq {
		slog.Warn("invalid connect request", "client_id", clientID, "error", err, "packet_type", packet.Type, "client_ip", clientIP, "client_port", clientPort, "remote_addr", remoteAddr)
		return
	}
	var request session.ConnectRequest
	if err := protocol.UnmarshalBSON(packet.Payload, &request); err != nil {
		slog.Warn("decode connect request failed", "client_id", clientID, "error", err, "client_ip", clientIP, "client_port", clientPort, "remote_addr", remoteAddr)
		return
	}
	if err := session.ValidateRequest(request, clientEdKey); err != nil {
		slog.Warn("validate connect request failed", "client_id", clientID, "error", err, "client_ip", clientIP, "client_port", clientPort, "remote_addr", remoteAddr)
		return
	}
	// Short-lived TLS session used only to fetch an update package (does not join the hub).
	if request.UpdateDownload {
		response, buildErr := session.BuildResponse(request, serverIdentity.PublicKey, configManager.Current(), time.Now())
		if buildErr != nil {
			return
		}
		response.ConnectionMode = "pull"
		response.ConnectAllowed = true
		// Omit config snapshot to keep the handshake small for download sessions.
		response.Config = nil
		response.ConfigUpdated = false
		payload, marshalErr := protocol.MarshalBSON(response)
		if marshalErr != nil {
			return
		}
		if writeErr := protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.ConnectResp, packet.Seq, 0, payload).WritePacket(conn); writeErr != nil {
			return
		}
		serveUpdateDownloadSession(ctx, conn, clientID, clientIP, clientPort, remoteAddr, preferences, serverIdentity)
		return
	}
	registry.UpdateClientVersion(clientEdKey, request.ClientVersion)
	listenerAddress := ""
	if host, _, splitErr := net.SplitHostPort(conn.RemoteAddr().String()); splitErr == nil {
		listenerAddress = host
	}
	registry.UpdateListenerCapability(clientEdKey, request.ListenerMode, int(request.ListenerPort), listenerAddress, time.Now())
	registry.MarkSeen(clientEdKey, time.Now())
	store.AddClientToBroadcasts(clientID, time.Now())
	response, err := session.BuildResponse(request, serverIdentity.PublicKey, configManager.Current(), time.Now())
	if err != nil {
		return
	}
	response.ConnectionMode = registry.ConnectionModeFor(clientID)
	response.ConnectAllowed = session.ReconnectAllowed(response.ConnectionMode, request.ReconnectProbe, request.ManualConnect)
	payload, err := protocol.MarshalBSON(response)
	if err != nil {
		return
	}
	if err := protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.ConnectResp, packet.Seq, 0, payload).WritePacket(conn); err != nil {
		return
	}
	if !response.ConnectAllowed {
		slog.Info("client reconnect probe denied", "client_id", clientID, "connection_mode", response.ConnectionMode, "client_ip", clientIP, "client_port", clientPort, "remote_addr", remoteAddr)
		return
	}
	slog.Info("client connected",
		"client_id", clientID,
		"client_ip", clientIP,
		"client_port", clientPort,
		"remote_addr", remoteAddr,
		"connection_mode", response.ConnectionMode,
		"client_version", request.ClientVersion,
	)
	unregister := hub.Register(clientID, conn)
	registry.ClearSessionEnd(clientID, time.Now())
	managementEvents.publish("devices")
	// intentionalEnd is set when the client sends ClientSessionEnd before closing.
	intentionalEnd := false
	defer func() {
		unregister()
		if !intentionalEnd {
			// Unexplained drop: start 30s grace; FinalizeUnexpectedDisconnects promotes later.
			registry.NoteUnexpectedDisconnect(clientID, time.Now())
			slog.Info("client disconnected unexpectedly",
				"client_id", clientID,
				"client_ip", clientIP,
				"client_port", clientPort,
				"remote_addr", remoteAddr,
			)
		} else {
			slog.Info("client disconnected intentionally",
				"client_id", clientID,
				"client_ip", clientIP,
				"client_port", clientPort,
				"remote_addr", remoteAddr,
			)
		}
		managementEvents.publish("devices")
	}()
	// Catch up mandatory (force) updates until the client reports the target client_version
	// (or, when the package has no client_version, until the offered seq is reached).
	if preferences != nil {
		if forceRecord, ok := preferences.latestPublishedForceUpdate(); ok {
			lastSeq := preferences.clientUpdateSeq(clientID)
			needCatchUp := forceRecord.Seq > lastSeq
			if forceRecord.ClientVersion != "" && request.ClientVersion != "" && request.ClientVersion != forceRecord.ClientVersion {
				// Client still runs an older build after a failed/skipped apply — re-offer.
				needCatchUp = true
			}
			if needCatchUp {
				metaInput := publishUpdateRequest{
					Component: forceRecord.Component, Components: append([]string(nil), forceRecord.Components...),
					Version: forceRecord.Version, ClientVersion: forceRecord.ClientVersion, UpdaterVersion: forceRecord.UpdaterVersion,
					Platform: forceRecord.Platform, PayloadFormat: "files-v1", SHA256: forceRecord.SHA256, Force: true,
				}
				if len(metaInput.Components) == 0 {
					if comps, primary, normErr := normalizeUpdateComponents(nil, forceRecord.Component); normErr == nil {
						metaInput.Components = comps
						metaInput.Component = primary
					}
				}
				notify, buildErr := buildUpdateAvailablePayload(metaInput, forceRecord, clientID, nil, serverIdentity.PrivateKey, preferences)
				if buildErr == nil {
					if payload, marshalErr := protocol.MarshalBSON(notify); marshalErr == nil {
						if sendErr := hub.Send(clientID, protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.UpdateAvailable, 0, 0, payload)); sendErr == nil {
							_ = preferences.markClientUpdateOffered(clientID, forceRecord)
							slog.Info("force update catch-up sent",
								"client_id", clientID,
								"seq", forceRecord.Seq,
								"sha256", forceRecord.SHA256,
								"version", forceRecord.Version,
								"client_version", request.ClientVersion,
								"target_client_version", forceRecord.ClientVersion,
							)
						}
					}
				} else {
					slog.Warn("force update catch-up skipped", "client_id", clientID, "error", buildErr)
				}
			}
		}
	}
	configEvents, unsubscribe := configManager.Subscribe()
	defer unsubscribe()
	sendConfigUpdate := func(updated config.Snapshot) bool {
		configPayload, marshalErr := protocol.MarshalBSON(updated)
		if marshalErr != nil {
			return false
		}
		if hub.Send(clientID, protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.ConfigUpdate, 0, 0, configPayload)) != nil {
			return false
		}
		return true
	}
	for {
		select {
		case updated, ok := <-configEvents:
			if !ok {
				return
			}
			if !sendConfigUpdate(updated) {
				return
			}
		default:
		}
		_ = conn.SetReadDeadline(time.Now().Add(time.Second))
		packet, err := protocol.ReadPacket(conn)
		if err != nil {
			if timeout, ok := err.(net.Error); ok && timeout.Timeout() {
				select {
				case <-ctx.Done():
					return
				default:
					continue
				}
			}
			if errors.Is(err, io.EOF) {
				return
			}
			return
		}
		if packet.Type == protocol.Ping {
			// 只有前端勾选 immediate 时 configManager.Publish 才会进入
			// configEvents；普通心跳不能绕过 snapshot 策略提前推送配置。
			_ = hub.Send(clientID, protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.Pong, packet.Seq, 0, packet.Payload))
		} else if packet.Type == protocol.MessageAck {
			var acknowledgement messageAck
			if protocol.UnmarshalBSON(packet.Payload, &acknowledgement) == nil {
				if messageID, parseErr := broadcast.ParseMessageID(acknowledgement.MessageID); parseErr == nil {
					if store.Acknowledge(messageID, clientID, broadcast.DeliveryStatus(acknowledgement.Status)) {
						slog.Info("message delivery acknowledgement", "message_id", acknowledgement.MessageID, "client_id", clientID, "status", acknowledgement.Status)
						managementEvents.publish("messages")
					}
				}
			}
		} else if packet.Type == protocol.ClientLog {
			var uploaded struct {
				Entry string `bson:"entry"`
			}
			if protocol.UnmarshalBSON(packet.Payload, &uploaded) == nil {
				if clientLogs.append(clientID, uploaded.Entry) {
					managementEvents.publish("client_logs:" + clientID)
				}
			}
		} else if packet.Type == protocol.ClientSessionEnd {
			var goodbye struct {
				Reason string `bson:"reason"`
				Detail string `bson:"detail"`
			}
			if protocol.UnmarshalBSON(packet.Payload, &goodbye) != nil {
				goodbye.Reason = session.SessionEndUserExit
			}
			reason := strings.TrimSpace(goodbye.Reason)
			if reason == "" {
				reason = session.SessionEndUserExit
			}
			registry.RecordIntentionalSessionEnd(clientID, reason, strings.TrimSpace(goodbye.Detail), time.Now())
			intentionalEnd = true
			slog.Info("client session end reported",
				"client_id", clientID,
				"reason", reason,
				"detail", goodbye.Detail,
				"client_ip", clientIP,
			)
			managementEvents.publish("devices")
			// Client will close shortly; end the read loop.
			return
		}
	}
}

type messageEnvelope struct {
	MessageID                   string                 `bson:"message_id"`
	QueueSeq                    uint64                 `bson:"queue_seq"`
	Priority                    uint16                 `bson:"priority"`
	Content                     string                 `bson:"content"`
	CreatedAt                   int64                  `bson:"created_at"`
	ExpiresAt                   int64                  `bson:"expires_at"`
	DisplayPosition             *string                `bson:"display_position,omitempty"`
	DisplayDurationRatio        *float64               `bson:"display_duration_ratio,omitempty"`
	AsDefault                   bool                   `bson:"as_default"`
	DefaultDisplayPosition      string                 `bson:"default_display_position,omitempty"`
	DefaultDisplayDurationRatio float64                `bson:"default_display_duration_ratio"`
	Speech                      []broadcast.SpeechNode `bson:"speech,omitempty"`
}

type messageAck struct {
	MessageID string `bson:"message_id"`
	Status    string `bson:"status"`
	Error     string `bson:"error,omitempty"`
}

func dispatchMessages(ctx context.Context, store *broadcast.Store, hub *session.Hub, configManager *config.Manager, registry *session.Registry) {
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			if store.Expire(now) > 0 {
				managementEvents.publish("messages")
			}
			ackTimeout := configManager.Current().AckTimeout
			if ackTimeout <= 0 {
				ackTimeout = config.DefaultAckTimeout
			}
			store.QueueDueRetries(now, ackTimeout)
			record := store.NextPending(now)
			if record == nil {
				continue
			}
			targets := make([]string, 0, len(record.TargetClients))
			wildcard := false
			for _, target := range record.TargetClients {
				if target == "*" {
					wildcard = true
					continue
				}
				if target != "" {
					targets = append(targets, target)
				}
			}
			if wildcard {
				for _, approved := range registry.ApprovedClientIDs() {
					store.AddClientToBroadcasts(approved, now)
					if !containsString(targets, approved) {
						targets = append(targets, approved)
					}
				}
				// Refresh delivery map after expanding newly approved clients.
				for _, item := range store.List(now) {
					if item.Message.ID == record.Message.ID {
						copy := item
						record = &copy
						break
					}
				}
			}
			if len(targets) == 0 {
				_ = store.RetryAfter(record.Message.ID, now.Add(5*time.Second))
				continue
			}
			currentConfig := configManager.Current()
			payload, err := protocol.MarshalBSON(messageEnvelope{
				MessageID: record.Message.ID.String(), QueueSeq: record.Message.QueueSeq,
				Priority: record.Message.Priority,
				Content:  record.Message.DisplayText, CreatedAt: record.CreatedAt.UnixMilli(),
				ExpiresAt: record.ExpiresAt.UnixMilli(), Speech: record.Message.Speech,
				DisplayPosition:             record.Message.DisplayPosition,
				DisplayDurationRatio:        record.Message.DisplayDurationRatio,
				AsDefault:                   record.Message.AsDefault,
				DefaultDisplayPosition:      currentConfig.DisplayPosition,
				DefaultDisplayDurationRatio: currentConfig.DisplayDurationRatio,
			})
			if err != nil {
				_ = store.Retry(record.Message.ID)
				continue
			}
			packet := protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.Message, 0, 0, payload)
			allSent := true
			messageChanged := false
			offlinePending := 0
			for _, target := range targets {
				// 已经写入 sent/最终回执的目标无需在下一轮重复发送；只有 pending
				// 目标在断线恢复后继续补投，避免离线重试造成重复弹窗。
				if status, ok := record.Deliveries[target]; ok && status != broadcast.DeliveryPending {
					if !store.ShouldRedeliver(record.Message.ID, target, now, ackTimeout) {
						continue
					}
				}
				// Only attempt delivery on currently online TLS sessions.
				// Offline targets stay pending and are retried later without hub.Send or WARN spam.
				if !hub.Online(target) {
					offlinePending++
					allSent = false
					continue
				}
				if err := hub.Send(target, packet); err != nil {
					// Race: session dropped between Online check and Send.
					if errors.Is(err, session.ErrClientOffline) {
						offlinePending++
						allSent = false
						continue
					}
					slog.Warn("message delivery failed", "message_id", record.Message.ID.String(), "client_id", target, "error", err)
					allSent = false
				} else {
					if record.Deliveries[target] != broadcast.DeliverySent {
						messageChanged = true
					}
					if !store.MarkSent(record.Message.ID, target, now) {
						slog.Warn("message delivery bookkeeping failed", "message_id", record.Message.ID.String(), "client_id", target)
						allSent = false
					}
				}
			}
			if allSent {
				_ = store.Complete(record.Message.ID, now)
			} else {
				// Back off when waiting on offline clients so the 250ms ticker does not busy-poll hub.Send.
				delay := 5 * time.Second
				if offlinePending > 0 && offlinePending == len(targets) {
					delay = 15 * time.Second
				}
				_ = store.RetryAfter(record.Message.ID, now.Add(delay))
			}
			if messageChanged || allSent {
				managementEvents.publish("messages")
			}
		}
	}
}

func containsString(values []string, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}

func notifyWithdrawal(record broadcast.Record, hub *session.Hub) {
	targets := make(map[string]struct{}, len(record.TargetClients)+len(record.Deliveries))
	for _, target := range record.TargetClients {
		if target != "" && target != "*" {
			targets[target] = struct{}{}
		}
	}
	for target := range record.Deliveries {
		if target != "" && target != "*" {
			targets[target] = struct{}{}
		}
	}
	withdrawalPayload := map[string]any{
		"message_id": record.Message.ID.String(), "message": "消息已撤回",
		"content": record.Message.DisplayText,
	}
	if record.Message.DisplayDurationRatio != nil {
		withdrawalPayload["display_duration_ratio"] = *record.Message.DisplayDurationRatio
	}
	payload, err := protocol.MarshalBSON(withdrawalPayload)
	if err != nil {
		return
	}
	packet := protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.MessageWithdraw, 0, 0, payload)
	for target := range targets {
		// Withdrawals only need to reach live sessions; offline clients will not show the message.
		if !hub.Online(target) {
			continue
		}
		_ = hub.Send(target, packet)
	}
}

func portOf(address string) uint16 {
	_, port, err := net.SplitHostPort(address)
	if err != nil {
		return 0
	}
	var value uint64
	for _, c := range port {
		if c < '0' || c > '9' {
			return 0
		}
		value = value*10 + uint64(c-'0')
		if value > 65535 {
			return 0
		}
	}
	return uint16(value)
}

type createMessageRequest struct {
	MessageID            string                 `json:"message_id"`
	Priority             *uint16                `json:"priority"`
	Content              string                 `json:"content" binding:"required"`
	Speech               []broadcast.SpeechNode `json:"speech"`
	TargetClientIDs      []string               `json:"target_client_ids"`
	ExpiresAt            *time.Time             `json:"expires_at"`
	DisplayPosition      *string                `json:"display_position"`
	DisplayDurationRatio *float64               `json:"display_duration_ratio"`
}

type messageResponse struct {
	MessageID            string                              `json:"message_id"`
	QueueSeq             uint64                              `json:"queue_seq"`
	Priority             uint16                              `json:"priority"`
	Content              string                              `json:"content"`
	TargetClientIDs      []string                            `json:"target_client_ids"`
	Status               broadcast.DeliveryStatus            `json:"status"`
	CreatedAt            int64                               `json:"created_at"`
	ExpiresAt            int64                               `json:"expires_at"`
	DisplayPosition      *string                             `json:"display_position,omitempty"`
	DisplayDurationRatio *float64                            `json:"display_duration_ratio,omitempty"`
	TTSEnabled           bool                                `json:"tts_enabled"`
	Deliveries           map[string]broadcast.DeliveryStatus `json:"deliveries,omitempty"`
	DeliveryHistory      map[string]broadcast.DeliveryStatus `json:"delivery_history,omitempty"`
}

func newRouter(stores ...*broadcast.Store) *gin.Engine {
	store := broadcast.NewStore()
	if len(stores) > 0 && stores[0] != nil {
		store = stores[0]
	}
	manager, err := config.NewManager(time.Now())
	if err != nil {
		panic(err)
	}
	return newRouterWithConfig(store, session.NewRegistry(), session.NewHub(), manager)
}

type approveDeviceRequest struct {
	ClientID   string  `json:"client_id" binding:"required"`
	Label      string  `json:"label"`
	ForcedMode *string `json:"forced_mode"`
}

type publishUpdateRequest struct {
	Component      string   `json:"component"`
	Components     []string `json:"components,omitempty"`
	Version        string   `json:"version"`
	ClientVersion  string   `json:"client_version"`
	UpdaterVersion string   `json:"updater_version"`
	Platform       string   `json:"platform"`
	PayloadFormat  string   `json:"payload_format"`
	SHA256         string   `json:"sha256"`
	Targets        []string `json:"target_client_ids"`
	// Force marks a mandatory update: every approved client must receive it
	// (online fan-out + catch-up on connect until they reach this seq).
	Force      bool   `json:"force,omitempty"`
	Signature  string `json:"signature,omitempty"`   // deprecated
	ReleaseURL string `json:"release_url,omitempty"` // deprecated
}

type updateAvailablePayload struct {
	Component      string   `bson:"component"`
	Components     []string `bson:"components,omitempty"`
	Version        string   `bson:"version"`
	ClientVersion  string   `bson:"client_version,omitempty"`
	UpdaterVersion string   `bson:"updater_version,omitempty"`
	Platform       string   `bson:"platform"`
	PayloadFormat  string   `bson:"payload_format"`
	SHA256         string   `bson:"sha256"`
	Seq            uint64   `bson:"seq,omitempty"`
	Force          bool     `bson:"force,omitempty"`
	// DownloadToken is an Ed25519-signed claim used on a dedicated TLS download session
	// (UpdateDownloadReq over the same -tls-addr as the business connection).
	DownloadToken string `bson:"download_token,omitempty"`
	ExpiresAt     int64  `bson:"expires_at,omitempty"`
	// Package is deprecated: large packages are fetched via TLS UpdateDownloadReq.
	Package    []byte `bson:"package,omitempty"`
	ReleaseURL string `bson:"release_url,omitempty"` // deprecated compatibility
	// DownloadURL is deprecated (HTTP path removed); kept for older clients only if ever set.
	DownloadURL string `bson:"download_url,omitempty"`
}

type updateDownloadRequestPayload struct {
	SHA256 string `bson:"sha256"`
	Token  string `bson:"token"`
}

type updateDownloadResponsePayload struct {
	SHA256    string `bson:"sha256"`
	Offset    int64  `bson:"offset"`
	TotalSize int64  `bson:"total_size"`
	Done      bool   `bson:"done"`
	Data      []byte `bson:"data,omitempty"`
	Error     string `bson:"error,omitempty"`
}

type updateDownloadClaims struct {
	SHA256    string
	ClientID  string
	ExpiresAt int64
	Nonce     string
}

func signUpdateDownloadToken(privateKey ed25519.PrivateKey, claims updateDownloadClaims) (string, error) {
	if len(privateKey) == 0 || claims.SHA256 == "" || claims.ClientID == "" || claims.ExpiresAt <= 0 {
		return "", errors.New("invalid update download claims")
	}
	if claims.Nonce == "" {
		nonce := make([]byte, 12)
		if _, err := rand.Read(nonce); err != nil {
			return "", err
		}
		claims.Nonce = hex.EncodeToString(nonce)
	}
	payload := strings.Join([]string{
		updateDownloadDomain,
		strings.ToLower(claims.SHA256),
		claims.ClientID,
		strconv.FormatInt(claims.ExpiresAt, 10),
		claims.Nonce,
	}, "\n")
	signature := ed25519.Sign(privateKey, []byte(payload))
	token := base64.RawURLEncoding.EncodeToString([]byte(payload)) + "." + base64.RawURLEncoding.EncodeToString(signature)
	return token, nil
}

func verifyUpdateDownloadToken(publicKey ed25519.PublicKey, token string, now time.Time) (updateDownloadClaims, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 2 {
		return updateDownloadClaims{}, errors.New("invalid download token")
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return updateDownloadClaims{}, errors.New("invalid download token payload")
	}
	signature, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return updateDownloadClaims{}, errors.New("invalid download token signature")
	}
	if !ed25519.Verify(publicKey, payload, signature) {
		return updateDownloadClaims{}, errors.New("download token signature mismatch")
	}
	fields := strings.Split(string(payload), "\n")
	if len(fields) != 5 || fields[0] != updateDownloadDomain {
		return updateDownloadClaims{}, errors.New("invalid download token domain")
	}
	expiresAt, err := strconv.ParseInt(fields[3], 10, 64)
	if err != nil || expiresAt <= 0 {
		return updateDownloadClaims{}, errors.New("invalid download token expiry")
	}
	if now.Unix() > expiresAt {
		return updateDownloadClaims{}, errors.New("download token expired")
	}
	claims := updateDownloadClaims{
		SHA256:    strings.ToLower(fields[1]),
		ClientID:  fields[2],
		ExpiresAt: expiresAt,
		Nonce:     fields[4],
	}
	if len(claims.SHA256) != 64 || claims.ClientID == "" {
		return updateDownloadClaims{}, errors.New("invalid download token claims")
	}
	return claims, nil
}

func mintUpdateDownloadToken(sha256Hex, clientID string, privateKey ed25519.PrivateKey, now time.Time) (token string, expiresAt int64, err error) {
	expiresAt = now.Add(updateDownloadTTL).Unix()
	token, err = signUpdateDownloadToken(privateKey, updateDownloadClaims{
		SHA256:    strings.ToLower(sha256Hex),
		ClientID:  clientID,
		ExpiresAt: expiresAt,
	})
	return token, expiresAt, err
}

// serveUpdateDownloadSession handles a short-lived TLS session that only fetches a package.
func serveUpdateDownloadSession(
	ctx context.Context,
	conn *tls.Conn,
	clientID string,
	clientIP, clientPort, remoteAddr string,
	preferences *serverPreferences,
	serverIdentity identity.Identity,
) {
	slog.Info("update download session opened",
		"client_id", clientID,
		"client_ip", clientIP,
		"client_port", clientPort,
		"remote_addr", remoteAddr,
	)
	defer slog.Info("update download session closed",
		"client_id", clientID,
		"client_ip", clientIP,
		"client_port", clientPort,
		"remote_addr", remoteAddr,
	)
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Minute))
	packet, err := protocol.ReadPacket(conn)
	if err != nil {
		slog.Warn("update download read failed", "client_id", clientID, "error", err)
		return
	}
	if packet.Type != protocol.UpdateDownloadReq {
		payload, _ := protocol.MarshalBSON(updateDownloadResponsePayload{Error: "expected update_download_req"})
		_ = protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.UpdateDownloadResp, packet.Seq, 0, payload).WritePacket(conn)
		return
	}
	var req updateDownloadRequestPayload
	if err := protocol.UnmarshalBSON(packet.Payload, &req); err != nil {
		payload, _ := protocol.MarshalBSON(updateDownloadResponsePayload{Error: "invalid update download request"})
		_ = protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.UpdateDownloadResp, packet.Seq, 0, payload).WritePacket(conn)
		return
	}
	sendErr := func(message string) {
		payload, _ := protocol.MarshalBSON(updateDownloadResponsePayload{
			SHA256: strings.ToLower(req.SHA256),
			Error:  message,
			Done:   true,
		})
		_ = protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.UpdateDownloadResp, packet.Seq, 0, payload).WritePacket(conn)
	}
	if preferences == nil || len(serverIdentity.PublicKey) == 0 {
		sendErr("update download unavailable")
		return
	}
	claims, err := verifyUpdateDownloadToken(serverIdentity.PublicKey, req.Token, time.Now())
	if err != nil {
		sendErr(err.Error())
		return
	}
	if claims.ClientID != clientID {
		sendErr("download token client mismatch")
		return
	}
	if claims.SHA256 != strings.ToLower(strings.TrimSpace(req.SHA256)) {
		sendErr("download token sha256 mismatch")
		return
	}
	path, record, pathErr := preferences.packagePathBySHA(claims.SHA256)
	if pathErr != nil || record.Status != "published" {
		sendErr("update package not found")
		return
	}
	file, openErr := os.Open(path)
	if openErr != nil {
		sendErr("could not open update package")
		return
	}
	defer file.Close()
	info, statErr := file.Stat()
	if statErr != nil {
		sendErr("could not stat update package")
		return
	}
	total := info.Size()
	buf := make([]byte, protocol.UpdateDownloadChunkSize)
	var offset int64
	var chunks int
	startedAt := time.Now()
	lastProgressLog := startedAt
	slog.Info("update download streaming start",
		"client_id", clientID,
		"sha256", claims.SHA256,
		"total_bytes", total,
		"chunk_size", protocol.UpdateDownloadChunkSize,
		"client_ip", clientIP,
	)
	_ = conn.SetWriteDeadline(time.Now().Add(30 * time.Minute))
	for {
		select {
		case <-ctx.Done():
			slog.Warn("update download cancelled",
				"client_id", clientID,
				"sha256", claims.SHA256,
				"offset", offset,
				"total_bytes", total,
				"chunks", chunks,
			)
			return
		default:
		}
		n, readErr := file.Read(buf)
		if n > 0 {
			chunk := make([]byte, n)
			copy(chunk, buf[:n])
			payload, marshalErr := protocol.MarshalBSON(updateDownloadResponsePayload{
				SHA256:    claims.SHA256,
				Offset:    offset,
				TotalSize: total,
				Done:      false,
				Data:      chunk,
			})
			if marshalErr != nil {
				slog.Warn("update download marshal failed", "client_id", clientID, "offset", offset, "error", marshalErr)
				return
			}
			// Refresh write deadline per chunk so a mid-transfer stall still surfaces.
			_ = conn.SetWriteDeadline(time.Now().Add(2 * time.Minute))
			if writeErr := protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.UpdateDownloadResp, packet.Seq, 0, payload).WritePacket(conn); writeErr != nil {
				slog.Warn("update download chunk write failed",
					"client_id", clientID,
					"offset", offset,
					"chunk_bytes", n,
					"total_bytes", total,
					"error", writeErr,
				)
				return
			}
			offset += int64(n)
			chunks++
			now := time.Now()
			if chunks == 1 || offset == total || now.Sub(lastProgressLog) >= 5*time.Second {
				elapsed := now.Sub(startedAt).Seconds()
				rate := float64(0)
				if elapsed > 0 {
					rate = float64(offset) / elapsed / 1024.0
				}
				pct := float64(0)
				if total > 0 {
					pct = 100.0 * float64(offset) / float64(total)
				}
				slog.Info("update download progress",
					"client_id", clientID,
					"sha256", claims.SHA256,
					"offset", offset,
					"total_bytes", total,
					"pct", pct,
					"rate_kib_s", rate,
					"chunks", chunks,
				)
				lastProgressLog = now
			}
		}
		if errors.Is(readErr, io.EOF) {
			payload, marshalErr := protocol.MarshalBSON(updateDownloadResponsePayload{
				SHA256:    claims.SHA256,
				Offset:    offset,
				TotalSize: total,
				Done:      true,
			})
			if marshalErr != nil {
				return
			}
			_ = conn.SetWriteDeadline(time.Now().Add(2 * time.Minute))
			if writeErr := protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.UpdateDownloadResp, packet.Seq, 0, payload).WritePacket(conn); writeErr != nil {
				slog.Warn("update download final frame write failed", "client_id", clientID, "offset", offset, "error", writeErr)
				return
			}
			elapsed := time.Since(startedAt).Seconds()
			rate := float64(0)
			if elapsed > 0 {
				rate = float64(offset) / elapsed / 1024.0
			}
			slog.Info("update download completed",
				"client_id", clientID,
				"sha256", claims.SHA256,
				"bytes", offset,
				"chunks", chunks,
				"elapsed_s", elapsed,
				"rate_kib_s", rate,
				"client_ip", clientIP,
			)
			return
		}
		if readErr != nil {
			slog.Warn("update download file read failed", "client_id", clientID, "offset", offset, "error", readErr)
			sendErr("read update package failed")
			return
		}
	}
}

type updatePackageMetadata struct {
	Component      string   `json:"component"`
	Components     []string `json:"components"`
	Version        string   `json:"version"`
	ClientVersion  string   `json:"client_version"`
	UpdaterVersion string   `json:"updater_version"`
	Platform       string   `json:"platform"`
	PayloadFormat  string   `json:"payload_format"`
	SHA256         string   `json:"sha256"`
}

// normalizeUpdateComponents returns the canonical list (updater before client).
// Accepts either metadata.components or legacy metadata.component / "bundle".
func normalizeUpdateComponents(components []string, legacy string) ([]string, string, error) {
	allowed := map[string]struct{}{"client": {}, "updater": {}}
	seen := make(map[string]struct{}, 2)
	out := make([]string, 0, 2)
	appendOne := func(raw string) error {
		item := strings.ToLower(strings.TrimSpace(raw))
		if item == "" {
			return nil
		}
		if item == "bundle" {
			for _, name := range []string{"updater", "client"} {
				if _, ok := seen[name]; !ok {
					seen[name] = struct{}{}
					out = append(out, name)
				}
			}
			return nil
		}
		if _, ok := allowed[item]; !ok {
			return fmt.Errorf("unsupported update component %q", raw)
		}
		if _, ok := seen[item]; !ok {
			seen[item] = struct{}{}
			out = append(out, item)
		}
		return nil
	}
	for _, item := range components {
		if err := appendOne(item); err != nil {
			return nil, "", err
		}
	}
	if len(out) == 0 {
		if err := appendOne(legacy); err != nil {
			return nil, "", err
		}
	}
	if len(out) == 0 {
		return nil, "", errors.New("update components is required")
	}
	rank := map[string]int{"updater": 0, "client": 1}
	sort.SliceStable(out, func(i, j int) bool { return rank[out[i]] < rank[out[j]] })
	primary := out[0]
	if len(out) > 1 {
		primary = "bundle"
	}
	return out, primary, nil
}

type updateManifestEntry struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
	Size   int    `json:"size"`
}

// marshalFilesV1Manifest builds the canonical files-v1 JSON used for metadata.sha256.
// Matches scripts/package_update.py, client/core/updater.py and updater/main.go:
// compact UTF-8 array, entries sorted by path string, object keys sorted (path, sha256, size).
func marshalFilesV1Manifest(entries []updateManifestEntry) ([]byte, error) {
	sorted := append([]updateManifestEntry(nil), entries...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].Path < sorted[j].Path })
	canonical := make([]map[string]interface{}, 0, len(sorted))
	for _, entry := range sorted {
		canonical = append(canonical, map[string]interface{}{
			"path":   entry.Path,
			"sha256": entry.SHA256,
			"size":   entry.Size,
		})
	}
	return json.Marshal(canonical)
}

func normalizeUpdateMemberPath(name string) string {
	name = strings.ReplaceAll(name, "\\", "/")
	name = strings.TrimPrefix(name, "./")
	return strings.TrimSuffix(name, "/")
}

const maxUpdatePackageBytes = protocol.MaxPayloadSize

type updateArchiveMember struct {
	name string
	dir  bool
	// diskPath is a temporary extracted file for regular members.
	diskPath string
	size     int64
}

func cleanupUpdateMembers(members []updateArchiveMember) {
	for _, member := range members {
		if member.diskPath != "" {
			_ = os.Remove(member.diskPath)
		}
	}
}

func isUnsafeUpdatePath(name string) bool {
	name = normalizeUpdateMemberPath(name)
	if name == "" || strings.HasPrefix(name, "/") {
		return true
	}
	for _, part := range strings.Split(name, "/") {
		if part == "" || part == "." || part == ".." {
			return true
		}
	}
	return false
}

func streamCopyLimited(dst io.Writer, src io.Reader, limit int64) (int64, error) {
	written, err := io.Copy(dst, io.LimitReader(src, limit+1))
	if err != nil {
		return written, err
	}
	if written > limit {
		return written, errors.New("update archive file exceeds package size limit")
	}
	return written, nil
}

func extractTarMembers(reader io.Reader, workDir string) ([]updateArchiveMember, error) {
	tarReader := tar.NewReader(reader)
	members := make([]updateArchiveMember, 0)
	index := 0
	for {
		header, err := tarReader.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			cleanupUpdateMembers(members)
			return nil, fmt.Errorf("read update tar: %w", err)
		}
		switch header.Typeflag {
		case tar.TypeDir:
			members = append(members, updateArchiveMember{name: header.Name, dir: true})
		case tar.TypeReg, tar.TypeRegA:
			tempPath := filepath.Join(workDir, fmt.Sprintf("tar-%05d.bin", index))
			index++
			out, createErr := os.OpenFile(tempPath, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
			if createErr != nil {
				cleanupUpdateMembers(members)
				return nil, createErr
			}
			written, copyErr := streamCopyLimited(out, tarReader, maxUpdatePackageBytes)
			closeErr := out.Close()
			if copyErr != nil || closeErr != nil {
				_ = os.Remove(tempPath)
				cleanupUpdateMembers(members)
				if copyErr != nil {
					return nil, copyErr
				}
				return nil, closeErr
			}
			members = append(members, updateArchiveMember{name: header.Name, diskPath: tempPath, size: written})
		default:
			cleanupUpdateMembers(members)
			return nil, fmt.Errorf("unsupported update tar entry: %s", header.Name)
		}
	}
	return members, nil
}

func sniffUpdateMagic(path string) ([4]byte, error) {
	file, err := os.Open(path)
	if err != nil {
		return [4]byte{}, err
	}
	defer file.Close()
	var magic [4]byte
	n, err := io.ReadFull(file, magic[:])
	if err != nil && !errors.Is(err, io.ErrUnexpectedEOF) && !errors.Is(err, io.EOF) {
		return [4]byte{}, err
	}
	if n < 4 {
		return magic, nil
	}
	return magic, nil
}

func hashFileSHA256(path string) (string, int64, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", 0, err
	}
	defer file.Close()
	digest := sha256.New()
	written, err := io.Copy(digest, file)
	if err != nil {
		return "", 0, err
	}
	return hex.EncodeToString(digest.Sum(nil)), written, nil
}

// metadataFromUpdatePackage reads only metadata.json from a .tar.zst without hashing payload files.
// Used on the publish hot path so large packages return immediately; full SHA-256 runs async.
func metadataFromUpdatePackage(packagePath string) (publishUpdateRequest, error) {
	magic, err := sniffUpdateMagic(packagePath)
	if err != nil {
		return publishUpdateRequest{}, err
	}
	if magic != [4]byte{0x28, 0xb5, 0x2f, 0xfd} {
		return publishUpdateRequest{}, errors.New("update package must be .tar.zst (zstd-compressed tar)")
	}
	file, openErr := os.Open(packagePath)
	if openErr != nil {
		return publishUpdateRequest{}, openErr
	}
	defer file.Close()
	decoder, decErr := zstd.NewReader(file)
	if decErr != nil {
		return publishUpdateRequest{}, fmt.Errorf("open zstd update package: %w", decErr)
	}
	defer decoder.Close()
	tarReader := tar.NewReader(decoder)
	var metadata updatePackageMetadata
	var found bool
	for {
		header, nextErr := tarReader.Next()
		if errors.Is(nextErr, io.EOF) {
			break
		}
		if nextErr != nil {
			return publishUpdateRequest{}, fmt.Errorf("update package is not a valid tar inside zstd: %w", nextErr)
		}
		name := normalizeUpdateMemberPath(header.Name)
		if name != "metadata.json" || header.Typeflag == tar.TypeDir {
			continue
		}
		if header.Size > 64<<10 {
			return publishUpdateRequest{}, errors.New("update metadata.json is too large")
		}
		limited := io.LimitReader(tarReader, header.Size+1)
		raw, readErr := io.ReadAll(limited)
		if readErr != nil {
			return publishUpdateRequest{}, readErr
		}
		if int64(len(raw)) > header.Size {
			return publishUpdateRequest{}, errors.New("update metadata.json is too large")
		}
		if err := json.Unmarshal(raw, &metadata); err != nil {
			return publishUpdateRequest{}, fmt.Errorf("invalid update metadata.json: %w", err)
		}
		found = true
		break
	}
	if !found {
		return publishUpdateRequest{}, errors.New("update archive metadata.json is missing")
	}
	return publishRequestFromPackageMetadata(metadata)
}

func publishRequestFromPackageMetadata(metadata updatePackageMetadata) (publishUpdateRequest, error) {
	components, primary, err := normalizeUpdateComponents(metadata.Components, metadata.Component)
	if err != nil {
		return publishUpdateRequest{}, err
	}
	hasClient, hasUpdater := false, false
	for _, name := range components {
		if name == "client" {
			hasClient = true
		}
		if name == "updater" {
			hasUpdater = true
		}
	}
	if hasClient && metadata.ClientVersion == "" {
		metadata.ClientVersion = metadata.Version
	}
	if hasUpdater && metadata.UpdaterVersion == "" {
		metadata.UpdaterVersion = metadata.Version
	}
	if metadata.SHA256 == "" {
		return publishUpdateRequest{}, errors.New("update metadata sha256 is required")
	}
	return publishUpdateRequest{
		Component: primary, Components: components, Version: metadata.Version, ClientVersion: metadata.ClientVersion,
		UpdaterVersion: metadata.UpdaterVersion, Platform: metadata.Platform, PayloadFormat: metadata.PayloadFormat,
		SHA256: strings.ToLower(metadata.SHA256),
	}, nil
}

// verifyUpdatePackageFile fully extracts a .tar.zst and checks files-v1 manifest SHA-256.
func verifyUpdatePackageFile(packagePath, workDir string) error {
	if err := os.MkdirAll(workDir, 0o700); err != nil {
		return err
	}
	magic, err := sniffUpdateMagic(packagePath)
	if err != nil {
		return err
	}
	if magic != [4]byte{0x28, 0xb5, 0x2f, 0xfd} {
		return errors.New("update package must be .tar.zst (zstd-compressed tar)")
	}
	extractDir := filepath.Join(workDir, "extract")
	if err := os.MkdirAll(extractDir, 0o700); err != nil {
		return err
	}
	file, openErr := os.Open(packagePath)
	if openErr != nil {
		return openErr
	}
	decoder, decErr := zstd.NewReader(file)
	if decErr != nil {
		_ = file.Close()
		return fmt.Errorf("open zstd update package: %w", decErr)
	}
	members, err := extractTarMembers(decoder, extractDir)
	decoder.Close()
	_ = file.Close()
	if err != nil {
		return fmt.Errorf("update package is not a valid tar inside zstd: %w", err)
	}
	defer cleanupUpdateMembers(members)

	var metadata updatePackageMetadata
	var metadataFound bool
	manifestEntries := make([]updateManifestEntry, 0, len(members))
	seen := make(map[string]struct{}, len(members))
	for _, member := range members {
		name := normalizeUpdateMemberPath(member.name)
		if name == "metadata.json" {
			if metadataFound {
				return errors.New("update archive contains duplicate metadata.json")
			}
			if member.size > 64<<10 {
				return errors.New("update metadata.json is too large")
			}
			raw, readErr := os.ReadFile(member.diskPath)
			if readErr != nil {
				return readErr
			}
			if err := json.Unmarshal(raw, &metadata); err != nil {
				return fmt.Errorf("invalid update metadata.json: %w", err)
			}
			metadataFound = true
			continue
		}
		if member.dir || name == "" {
			continue
		}
		if isUnsafeUpdatePath(name) {
			return fmt.Errorf("unsafe update archive path: %s", name)
		}
		if _, exists := seen[name]; exists {
			return fmt.Errorf("duplicate update archive path: %s", name)
		}
		seen[name] = struct{}{}
		digestHex, size, hashErr := hashFileSHA256(member.diskPath)
		if hashErr != nil {
			return hashErr
		}
		manifestEntries = append(manifestEntries, updateManifestEntry{Path: name, SHA256: digestHex, Size: int(size)})
	}
	if !metadataFound {
		return errors.New("update archive metadata.json is missing")
	}
	if _, err := publishRequestFromPackageMetadata(metadata); err != nil {
		return err
	}
	manifest, err := marshalFilesV1Manifest(manifestEntries)
	if err != nil {
		return err
	}
	manifestDigest := sha256.Sum256(manifest)
	computedSHA := hex.EncodeToString(manifestDigest[:])
	if metadata.SHA256 == "" {
		return errors.New("update metadata sha256 is required")
	}
	if !strings.EqualFold(metadata.SHA256, computedSHA) {
		return fmt.Errorf("update metadata sha256 does not match package contents (expected %s, got %s)", strings.ToLower(metadata.SHA256), computedSHA)
	}
	return nil
}

// parseUpdatePackageFile is the synchronous full parse used by unit tests.
func parseUpdatePackageFile(packagePath, workDir string) (publishUpdateRequest, string, error) {
	input, err := metadataFromUpdatePackage(packagePath)
	if err != nil {
		return publishUpdateRequest{}, "", err
	}
	if err := verifyUpdatePackageFile(packagePath, workDir); err != nil {
		return publishUpdateRequest{}, "", err
	}
	normalizedPath := filepath.Join(workDir, "normalized.tar.zst")
	if err := copyFileStream(packagePath, normalizedPath); err != nil {
		return publishUpdateRequest{}, "", err
	}
	return input, normalizedPath, nil
}

// parseUpdatePackage keeps a small in-memory helper for unit tests.
func parseUpdatePackage(packageBytes []byte) (publishUpdateRequest, []byte, error) {
	workDir, err := os.MkdirTemp("", "mkcb-update-test-*")
	if err != nil {
		return publishUpdateRequest{}, nil, err
	}
	defer os.RemoveAll(workDir)
	source := filepath.Join(workDir, "upload.bin")
	if err := os.WriteFile(source, packageBytes, 0o600); err != nil {
		return publishUpdateRequest{}, nil, err
	}
	input, normalizedPath, err := parseUpdatePackageFile(source, workDir)
	if err != nil {
		return publishUpdateRequest{}, nil, err
	}
	normalized, err := os.ReadFile(normalizedPath)
	if err != nil {
		return publishUpdateRequest{}, nil, err
	}
	return input, normalized, nil
}

func validatePublishUpdate(input *publishUpdateRequest) error {
	if input == nil {
		return errors.New("update request is required")
	}
	components, primary, err := normalizeUpdateComponents(input.Components, input.Component)
	if err != nil {
		return err
	}
	input.Components = components
	input.Component = primary
	if input.Version == "" || len(input.Version) > 64 || strings.TrimSpace(input.Version) != input.Version {
		return errors.New("version is required and must be at most 64 characters")
	}
	if input.Platform != "windows-amd64" || input.PayloadFormat != "files-v1" {
		return errors.New("unsupported update platform or payload format")
	}
	digest, err := hex.DecodeString(input.SHA256)
	if err != nil || len(digest) != 32 {
		return errors.New("sha256 must contain 64 hexadecimal characters")
	}
	if input.ClientVersion == "" && input.UpdaterVersion == "" && input.Signature != "" {
		// Accept the former JSON test/client shape while transitioning to local packages.
		input.ClientVersion = input.Version
	}
	if input.ClientVersion == "" && input.UpdaterVersion == "" {
		return errors.New("client_version or updater_version is required")
	}
	if input.Force {
		// Mandatory updates always target every currently approved client.
		// Targets may still be empty at validate time; the publish handler expands them.
		return nil
	}
	if len(input.Targets) == 0 || len(input.Targets) > 1000 {
		return errors.New("select between 1 and 1000 target clients")
	}
	return nil
}

// expandForceUpdateTargets replaces Targets with every approved client id.
func expandForceUpdateTargets(registry *session.Registry, input *publishUpdateRequest) error {
	if input == nil || !input.Force || registry == nil {
		return nil
	}
	devices := registry.List()
	targets := make([]string, 0, len(devices))
	for _, device := range devices {
		if device.Status != "approved" {
			continue
		}
		targets = append(targets, device.ClientID)
	}
	if len(targets) == 0 {
		return errors.New("force update requires at least one approved client")
	}
	if len(targets) > 1000 {
		return errors.New("too many approved clients for one force update (max 1000)")
	}
	input.Targets = targets
	return nil
}

// latestPublishedForceUpdate returns the highest-seq published force update, if any.
func (p *serverPreferences) latestPublishedForceUpdate() (updateRecord, bool) {
	if p == nil || p.db == nil {
		return updateRecord{}, false
	}
	items, err := p.updateRecords()
	if err != nil {
		return updateRecord{}, false
	}
	var best updateRecord
	found := false
	for _, item := range items {
		if item.Status != "published" || !item.Force {
			continue
		}
		if !found || item.Seq > best.Seq {
			best = item
			found = true
		}
	}
	return best, found
}

// buildUpdateAvailablePayload builds a signed UPDATE_AVAILABLE for one client.
func buildUpdateAvailablePayload(input publishUpdateRequest, record updateRecord, clientID string, packageBytes []byte, privateKey ed25519.PrivateKey, preferences *serverPreferences) (updateAvailablePayload, error) {
	notify := updateAvailablePayload{
		Component: input.Component, Components: append([]string(nil), input.Components...),
		Version: input.Version, Platform: input.Platform,
		PayloadFormat: input.PayloadFormat, SHA256: strings.ToLower(input.SHA256),
		ClientVersion: input.ClientVersion, UpdaterVersion: input.UpdaterVersion,
		Seq: record.Seq, Force: record.Force || input.Force, ReleaseURL: input.ReleaseURL,
	}
	hasSigningKey := len(privateKey) > 0
	hasDurable := preferences != nil
	if hasDurable {
		if path, pathErr := preferences.updatePackagePath(record.SHA256); pathErr == nil {
			if _, statErr := os.Stat(path); statErr == nil {
				hasDurable = true
			} else {
				hasDurable = false
			}
		}
	}
	if hasSigningKey && hasDurable {
		token, expiresAt, tokenErr := mintUpdateDownloadToken(record.SHA256, clientID, privateKey, time.Now())
		if tokenErr != nil {
			return updateAvailablePayload{}, errors.New("could not mint update download token")
		}
		notify.DownloadToken = token
		notify.ExpiresAt = expiresAt
	} else if len(packageBytes) > 0 && len(packageBytes) <= 1<<20 {
		notify.Package = packageBytes
	} else if hasDurable && !hasSigningKey {
		return updateAvailablePayload{}, errors.New("server identity unavailable for signed download tokens")
	}
	return notify, nil
}

type renameDeviceRequest struct {
	Label      string  `json:"label"`
	ForcedMode *string `json:"forced_mode"`
}

type deviceResponse struct {
	session.Device
	Online              bool   `json:"online"`
	SessionEndReason    string `json:"session_end_reason,omitempty"`
	SessionEndLabel     string `json:"session_end_label,omitempty"`
	SessionEndAt        int64  `json:"session_end_at,omitempty"`
	SessionEndDetail    string `json:"session_end_detail,omitempty"`
}

type configUpdateRequest struct {
	HeartbeatIntervalSeconds     *int32   `json:"heartbeat_interval_seconds"`
	HeartbeatTimeoutSeconds      *int32   `json:"heartbeat_timeout_seconds"`
	MessageTTLHours              *int32   `json:"message_ttl_hours"`
	AckTimeoutSeconds            *int32   `json:"ack_timeout_seconds"`
	MaxSpeechDepth               *int32   `json:"max_speech_depth"`
	MaxRepeatExpansion           *int32   `json:"max_repeat_expansion"`
	DisplayPosition              *string  `json:"default_display_position"`
	DisplayDurationRatio         *float64 `json:"default_display_duration_ratio"`
	ListenerProbeIntervalSeconds *int32   `json:"listener_probe_interval_seconds"`
	ListenerProbeDurationHours   *int32   `json:"listener_probe_duration_hours"`
	ListenerProbeResetDays       *int32   `json:"listener_probe_reset_days"`
	ListenerLossThreshold        *int32   `json:"listener_loss_threshold"`
	ListenerIdleTimeoutSeconds   *int32   `json:"listener_idle_timeout_seconds"`
	Immediate                    bool     `json:"immediate"`
}

type configResponse struct {
	ConfigID              string  `json:"config_id"`
	IssuedAt              int64   `json:"issued_at"`
	HeartbeatInterval     int64   `json:"heartbeat_interval_seconds"`
	HeartbeatTimeout      int64   `json:"heartbeat_timeout_seconds"`
	MessageTTL            int64   `json:"message_ttl_hours"`
	AckTimeout            int64   `json:"ack_timeout_seconds"`
	MaxSpeechDepth        int32   `json:"max_speech_depth"`
	MaxRepeatExpansion    int32   `json:"max_repeat_expansion"`
	DisplayPosition       string  `json:"default_display_position"`
	DisplayDurationRatio  float64 `json:"display_duration_ratio"`
	ListenerProbeInterval int64   `json:"listener_probe_interval_seconds"`
	ListenerProbeDuration int64   `json:"listener_probe_duration_hours"`
	ListenerProbeReset    int64   `json:"listener_probe_reset_days"`
	ListenerLossThreshold int32   `json:"listener_loss_threshold"`
	ListenerIdleTimeout   int64   `json:"listener_idle_timeout_seconds"`
}

func newRouterWithConfig(store *broadcast.Store, registry *session.Registry, hub *session.Hub, configManager *config.Manager, _ ...string) *gin.Engine {
	return newRouterWithDependencies(store, registry, hub, configManager, nil, nil, nil, nil, identity.Identity{}, 39002, 39003, 39001)
}

func newRouterWithDependencies(store *broadcast.Store, registry *session.Registry, hub *session.Hub, configManager *config.Manager, preferences *serverPreferences, requestShutdown func(), logs *logBuffer, clientLogs *clientLogBuffer, serverIdentity identity.Identity, tlsPort ...uint16) *gin.Engine {
	listenTLSPort := uint16(39002)
	listenHTTPPort := uint16(39003)
	listenUDPPort := uint16(39001)
	if len(tlsPort) >= 1 && tlsPort[0] != 0 {
		listenTLSPort = tlsPort[0]
	}
	if len(tlsPort) >= 2 && tlsPort[1] != 0 {
		listenHTTPPort = tlsPort[1]
	}
	if len(tlsPort) >= 3 && tlsPort[2] != 0 {
		listenUDPPort = tlsPort[2]
	}
	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	router.Use(gin.Recovery())
	if logs == nil {
		logs = newLogBuffer(maxInMemoryLogLines)
	}
	if clientLogs == nil {
		clientLogs = newClientLogBuffer(500)
	}
	router.Use(func(c *gin.Context) {
		started := time.Now()
		c.Next()
		if c.Request.URL.Path != "/api/v1/events" {
			ip, port, remote := requestClientAddr(c)
			slog.Info("HTTP request",
				"method", c.Request.Method,
				"path", c.Request.URL.Path,
				"status", c.Writer.Status(),
				"duration_ms", time.Since(started).Milliseconds(),
				"client_ip", ip,
				"client_port", port,
				"remote_addr", remote,
			)
		}
	})
	frontendRoot, _ := fs.Sub(frontendFiles, "web")
	frontendHandler := http.FileServer(http.FS(frontendRoot))
	router.NoRoute(func(c *gin.Context) {
		// API 路径保持 JSON 404；其他 GET 路径交给嵌入式 Vue 应用处理。
		if (c.Request.Method != http.MethodGet && c.Request.Method != http.MethodHead) || strings.HasPrefix(c.Request.URL.Path, "/api/") {
			c.JSON(http.StatusNotFound, gin.H{"error": "not found"})
			return
		}
		frontendHandler.ServeHTTP(c.Writer, c.Request)
	})
	router.GET("/healthz", func(c *gin.Context) {
		c.JSON(http.StatusOK, currentHealth(listenTLSPort, listenHTTPPort, listenUDPPort))
	})
	router.GET("/api/v1/preferences", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"page_size": preferences.pageSize()})
	})
	router.POST("/api/v1/preferences", func(c *gin.Context) {
		var input struct {
			PageSize int `json:"page_size" binding:"required"`
		}
		if err := c.ShouldBindJSON(&input); err != nil || !validPageSize(input.PageSize) {
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid page size"})
			return
		}
		if err := preferences.setPageSize(input.PageSize); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
		managementEvents.publish("preferences")
		c.JSON(http.StatusOK, gin.H{"page_size": input.PageSize})
	})
	router.POST("/api/v1/shutdown", func(c *gin.Context) {
		if requestShutdown == nil {
			c.JSON(http.StatusNotImplemented, gin.H{"error": "controlled shutdown is unavailable"})
			return
		}
		requestShutdown()
		c.JSON(http.StatusAccepted, gin.H{"status": "shutdown_requested"})
	})
	// Manual retention purge for messages/devices older than 185 days.
	// Automatic job still runs on a 6h schedule; this endpoint is for admin maintenance UI.
	router.POST("/api/v1/maintenance/purge-expired", func(c *gin.Context) {
		result := purgeExpiredHistory(store, registry, time.Now())
		slog.Info("manual retention cleanup completed",
			"max_age_days", result.MaxAgeDays,
			"messages_removed", result.MessagesRemoved,
			"devices_removed", result.DevicesRemoved)
		c.JSON(http.StatusOK, result)
	})
	router.GET("/api/v1/health", func(c *gin.Context) {
		c.JSON(http.StatusOK, currentHealth(listenTLSPort, listenHTTPPort, listenUDPPort))
	})
	router.GET("/api/v1/events", func(c *gin.Context) {
		c.Header("Content-Type", "text/event-stream")
		c.Header("Cache-Control", "no-cache")
		c.Header("Connection", "keep-alive")
		c.Header("X-Accel-Buffering", "no")
		flusher, ok := c.Writer.(http.Flusher)
		if !ok {
			c.Status(http.StatusInternalServerError)
			return
		}
		events, unsubscribe := managementEvents.subscribe()
		defer unsubscribe()
		c.Writer.Write([]byte(": connected\n\n"))
		flusher.Flush()
		keepAlive := time.NewTicker(15 * time.Second)
		defer keepAlive.Stop()
		for {
			select {
			case <-c.Request.Context().Done():
				return
			case event, open := <-events:
				if !open {
					return
				}
				_, _ = c.Writer.Write([]byte("event: refresh\ndata: {\"section\":\"" + event + "\"}\n\n"))
				flusher.Flush()
			case <-keepAlive.C:
				_, _ = c.Writer.Write([]byte(": keepalive\n\n"))
				flusher.Flush()
			}
		}
	})
	router.GET("/api/v1/logs", func(c *gin.Context) {
		page := 1
		if value, err := strconv.Atoi(c.Query("page")); err == nil && value > 0 {
			page = value
		}
		pageSize := 100 // 后端默认每页 100 条，前端可以按共享分页偏好请求更小页面。
		if value, err := strconv.Atoi(c.Query("page_size")); err == nil && value > 0 {
			pageSize = value
		}
		if !validPageSize(pageSize) {
			c.JSON(http.StatusBadRequest, gin.H{"error": "log page size must be one of 10, 15, 20, 25, 30, 50, 100 or 200"})
			return
		}
		content, total, err := readLogPageFromLines(logs.snapshot(), page, pageSize, logPageFilter{
			Start:          c.Query("start"),
			End:            c.Query("end"),
			MinimumLevel:   c.Query("minimum_level"),
			SelectedLevels: c.Query("levels"),
		})
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
		c.JSON(http.StatusOK, gin.H{"content": content, "page": page, "page_size": pageSize, "total": total})
	})
	router.GET("/api/v1/config", func(c *gin.Context) {
		c.JSON(http.StatusOK, toConfigResponse(configManager.Current()))
	})
	router.POST("/api/v1/config", func(c *gin.Context) {
		var input configUpdateRequest
		if err := c.ShouldBindJSON(&input); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		update := config.Update{
			MaxSpeechDepth: input.MaxSpeechDepth, MaxRepeatExpansion: input.MaxRepeatExpansion,
			DisplayPosition: input.DisplayPosition, DisplayDurationRatio: input.DisplayDurationRatio,
		}
		if input.ListenerProbeIntervalSeconds != nil {
			value := time.Duration(*input.ListenerProbeIntervalSeconds) * time.Second
			update.ListenerProbeInterval = &value
		}
		if input.ListenerProbeDurationHours != nil {
			value := time.Duration(*input.ListenerProbeDurationHours) * time.Hour
			update.ListenerProbeDuration = &value
		}
		if input.ListenerProbeResetDays != nil {
			value := time.Duration(*input.ListenerProbeResetDays) * 24 * time.Hour
			update.ListenerProbeReset = &value
		}
		update.ListenerLossThreshold = input.ListenerLossThreshold
		if input.ListenerIdleTimeoutSeconds != nil {
			value := time.Duration(*input.ListenerIdleTimeoutSeconds) * time.Second
			update.ListenerIdleTimeout = &value
		}
		if input.HeartbeatIntervalSeconds != nil {
			value := time.Duration(*input.HeartbeatIntervalSeconds) * time.Second
			update.HeartbeatInterval = &value
		}
		if input.HeartbeatTimeoutSeconds != nil {
			value := time.Duration(*input.HeartbeatTimeoutSeconds) * time.Second
			update.HeartbeatTimeout = &value
		}
		if input.MessageTTLHours != nil {
			value := time.Duration(*input.MessageTTLHours) * time.Hour
			update.MessageTTL = &value
		}
		if input.AckTimeoutSeconds != nil {
			value := time.Duration(*input.AckTimeoutSeconds) * time.Second
			update.AckTimeout = &value
		}
		updated, err := configManager.Update(update, time.Now())
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		registry.SetListenerPolicy(session.ListenerPolicy{
			ProbeInterval: updated.ListenerProbeInterval,
			ProbeDuration: updated.ListenerProbeDuration,
			ProbeReset:    updated.ListenerProbeReset,
			LossThreshold: int(updated.ListenerLossThreshold),
			IdleTimeout:   updated.ListenerIdleTimeout,
		})
		if input.Immediate {
			configManager.Publish(updated)
		}
		managementEvents.publish("config")
		c.JSON(http.StatusOK, gin.H{"config": toConfigResponse(updated), "immediate": input.Immediate})
	})
	router.GET("/api/v1/devices", func(c *gin.Context) {
		items := registry.List()
		now := time.Now()
		// Promote grace-elapsed unexplained drops before building the response.
		onlineSet := make(map[string]struct{}, len(hub.OnlineIDs()))
		for _, id := range hub.OnlineIDs() {
			onlineSet[id] = struct{}{}
		}
		if registry.FinalizeUnexpectedDisconnects(onlineSet, now) > 0 {
			items = registry.List()
		}
		response := make([]deviceResponse, 0, len(items))
		for _, device := range items {
			online := hub.Online(device.ClientID)
			reason, label := session.SessionEndDisplay(device, online, now)
			response = append(response, deviceResponse{
				Device:           device,
				Online:           online,
				SessionEndReason: reason,
				SessionEndLabel:  label,
				SessionEndAt:     device.SessionEndAt,
				SessionEndDetail: device.SessionEndDetail,
			})
		}
		c.JSON(http.StatusOK, gin.H{"items": response})
	})
	// fanOutUpdateNotification pushes UPDATE_AVAILABLE after SHA-256 verification succeeds.
	fanOutUpdateNotification := func(input publishUpdateRequest, record updateRecord, packageBytes []byte) (sent, offline []string, err error) {
		if input.Force {
			if expandErr := expandForceUpdateTargets(registry, &input); expandErr != nil {
				return nil, nil, expandErr
			}
			record.Force = true
		}
		sent = make([]string, 0, len(input.Targets))
		offline = make([]string, 0)
		for _, target := range input.Targets {
			id, normalizeErr := session.NormalizeClientID(target)
			if normalizeErr != nil {
				return nil, nil, errors.New("invalid target client id")
			}
			device, getErr := registry.Get(id)
			if getErr != nil || device.Status != "approved" {
				return nil, nil, errors.New("all update targets must be approved clients")
			}
			notify, buildErr := buildUpdateAvailablePayload(input, record, id, packageBytes, serverIdentity.PrivateKey, preferences)
			if buildErr != nil {
				return nil, nil, buildErr
			}
			payload, marshalErr := protocol.MarshalBSON(notify)
			if marshalErr != nil {
				return nil, nil, errors.New("could not encode update metadata")
			}
			packet := protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.UpdateAvailable, 0, 0, payload)
			if sendErr := hub.Send(id, packet); sendErr != nil {
				offline = append(offline, id)
				// Do not mark offered while offline: force catch-up on connect uses last_seq < force.seq.
				continue
			}
			if preferences != nil {
				_ = preferences.markClientUpdateOffered(id, record)
			}
			sent = append(sent, id)
		}
		return sent, offline, nil
	}

	router.POST("/api/v1/updates/publish", func(c *gin.Context) {
		var input publishUpdateRequest
		var packageBytes []byte // tests / metadata-only JSON publish without file store
		var storedRecord *updateRecord
		asyncVerify := false

		if strings.HasPrefix(c.GetHeader("Content-Type"), "multipart/form-data") {
			// Stream package to disk; only read metadata.json (no full SHA-256) on the request path.
			if err := c.Request.ParseMultipartForm(8 << 20); err != nil {
				c.JSON(http.StatusBadRequest, gin.H{"error": "invalid update form"})
				return
			}
			if rawTargets := c.PostFormArray("target_client_ids"); len(rawTargets) > 0 {
				input.Targets = rawTargets
			} else if value := c.PostForm("target_client_ids"); value != "" {
				_ = json.Unmarshal([]byte(value), &input.Targets)
			}
			forceRaw := strings.TrimSpace(strings.ToLower(c.PostForm("force")))
			input.Force = forceRaw == "1" || forceRaw == "true" || forceRaw == "yes" || forceRaw == "on"
			file, _, err := c.Request.FormFile("package")
			if err != nil {
				c.JSON(http.StatusBadRequest, gin.H{"error": "package file is required"})
				return
			}
			defer file.Close()
			workRoot := "data"
			if preferences != nil && preferences.updatesDir != "" {
				workRoot = preferences.updatesDir
			}
			workDir, err := os.MkdirTemp(workRoot, ".upload-*")
			if err != nil {
				c.JSON(http.StatusInternalServerError, gin.H{"error": "could not create upload workspace"})
				return
			}
			uploadPath := filepath.Join(workDir, "upload.tar.zst")
			out, createErr := os.OpenFile(uploadPath, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
			if createErr != nil {
				_ = os.RemoveAll(workDir)
				c.JSON(http.StatusInternalServerError, gin.H{"error": "could not stage update package"})
				return
			}
			written, copyErr := streamCopyLimited(out, file, maxUpdatePackageBytes)
			closeErr := out.Close()
			if copyErr != nil || closeErr != nil || written == 0 {
				_ = os.RemoveAll(workDir)
				c.JSON(http.StatusBadRequest, gin.H{"error": "update package exceeds protocol limit or is empty"})
				return
			}
			metadataInput, metaErr := metadataFromUpdatePackage(uploadPath)
			if metaErr != nil {
				_ = os.RemoveAll(workDir)
				c.JSON(http.StatusBadRequest, gin.H{"error": metaErr.Error()})
				return
			}
			forceFlag := input.Force
			metadataInput.Targets = input.Targets
			metadataInput.Force = forceFlag
			input = metadataInput
			if err := validatePublishUpdate(&input); err != nil {
				_ = os.RemoveAll(workDir)
				c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
				return
			}
			if input.Force {
				if expandErr := expandForceUpdateTargets(registry, &input); expandErr != nil {
					_ = os.RemoveAll(workDir)
					c.JSON(http.StatusBadRequest, gin.H{"error": expandErr.Error()})
					return
				}
			}
			// Pre-validate targets so the admin gets an immediate error if selection is wrong.
			for _, target := range input.Targets {
				id, normalizeErr := session.NormalizeClientID(target)
				if normalizeErr != nil {
					_ = os.RemoveAll(workDir)
					c.JSON(http.StatusBadRequest, gin.H{"error": "invalid target client id"})
					return
				}
				device, getErr := registry.Get(id)
				if getErr != nil || device.Status != "approved" {
					_ = os.RemoveAll(workDir)
					c.JSON(http.StatusBadRequest, gin.H{"error": "all update targets must be approved clients"})
					return
				}
			}
			record := updateRecord{
				UpdateID: fmt.Sprintf("%d", time.Now().UnixNano()), Component: input.Component, Components: append([]string(nil), input.Components...),
				Version: input.Version, Platform: input.Platform, ClientVersion: input.ClientVersion, UpdaterVersion: input.UpdaterVersion,
				SHA256: strings.ToLower(input.SHA256), Status: "verifying", Force: input.Force, CreatedAt: time.Now().UnixMilli(),
			}
			if preferences != nil {
				seq, seqErr := preferences.allocateUpdateSeq()
				if seqErr != nil {
					_ = os.RemoveAll(workDir)
					c.JSON(http.StatusInternalServerError, gin.H{"error": "could not allocate update sequence"})
					return
				}
				record.Seq = seq
				metadata, _ := json.Marshal(input)
				if err := preferences.saveUpdateFile(record, metadata, uploadPath); err != nil {
					_ = os.RemoveAll(workDir)
					c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
					return
				}
				_ = os.RemoveAll(workDir)
				storedRecord = &record
				asyncVerify = true
			} else {
				// No persistence: verify synchronously so tests still fan-out in-process.
				verifyDir := filepath.Join(workDir, "verify")
				if err := verifyUpdatePackageFile(uploadPath, verifyDir); err != nil {
					_ = os.RemoveAll(workDir)
					c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
					return
				}
				record.Status = "published"
				record.Seq = 1
				packageBytes, err = os.ReadFile(uploadPath)
				_ = os.RemoveAll(workDir)
				if err != nil {
					c.JSON(http.StatusInternalServerError, gin.H{"error": "could not read update package"})
					return
				}
				storedRecord = &record
			}
		} else if err := c.ShouldBindJSON(&input); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid update request"})
			return
		} else {
			if err := validatePublishUpdate(&input); err != nil {
				c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
				return
			}
			if input.Force {
				if expandErr := expandForceUpdateTargets(registry, &input); expandErr != nil {
					c.JSON(http.StatusBadRequest, gin.H{"error": expandErr.Error()})
					return
				}
			}
			// JSON path has no package file; treat as already verified (legacy / unit tests).
			record := updateRecord{
				UpdateID: fmt.Sprintf("%d", time.Now().UnixNano()), Component: input.Component, Components: append([]string(nil), input.Components...),
				Version: input.Version, Platform: input.Platform, ClientVersion: input.ClientVersion, UpdaterVersion: input.UpdaterVersion,
				SHA256: strings.ToLower(input.SHA256), Status: "published", Force: input.Force, CreatedAt: time.Now().UnixMilli(),
			}
			if preferences != nil {
				seq, seqErr := preferences.allocateUpdateSeq()
				if seqErr != nil {
					c.JSON(http.StatusInternalServerError, gin.H{"error": "could not allocate update sequence"})
					return
				}
				record.Seq = seq
				metadata, _ := json.Marshal(input)
				if err := preferences.saveUpdate(record, metadata, nil); err != nil {
					c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
					return
				}
			} else {
				record.Seq = 1
			}
			storedRecord = &record
		}

		if storedRecord == nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "package file is required"})
			return
		}
		record := *storedRecord

		if asyncVerify && preferences != nil {
			// Return immediately after upload; SHA-256 + fan-out run in background.
			managementEvents.publish("updates")
			go func(upd publishUpdateRequest, rec updateRecord) {
				workRoot := preferences.updatesDir
				verifyDir, mkErr := os.MkdirTemp(workRoot, ".verify-*")
				if mkErr != nil {
					_ = preferences.setUpdateStatus(rec.UpdateID, "failed", "could not create verify workspace")
					managementEvents.publish("updates")
					return
				}
				defer os.RemoveAll(verifyDir)
				pkgPath, pathErr := preferences.updatePackagePath(rec.SHA256)
				if pathErr != nil {
					_ = preferences.setUpdateStatus(rec.UpdateID, "failed", pathErr.Error())
					managementEvents.publish("updates")
					return
				}
				if verifyErr := verifyUpdatePackageFile(pkgPath, verifyDir); verifyErr != nil {
					_ = preferences.setUpdateStatus(rec.UpdateID, "failed", verifyErr.Error())
					// Keep the bad package for admin inspection; download is blocked by status!=published.
					managementEvents.publish("updates")
					return
				}
				if setErr := preferences.setUpdateStatus(rec.UpdateID, "published", ""); setErr != nil {
					managementEvents.publish("updates")
					return
				}
				rec.Status = "published"
				rec.StatusDetail = ""
				_, _, _ = fanOutUpdateNotification(upd, rec, nil)
				managementEvents.publish("updates")
			}(input, record)
			c.JSON(http.StatusAccepted, gin.H{
				"update":  record,
				"sent":    []string{},
				"offline": []string{},
				"pending": true,
				"message": "uploaded; verifying package SHA-256 in background",
			})
			return
		}

		sent, offline, fanErr := fanOutUpdateNotification(input, record, packageBytes)
		if fanErr != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": fanErr.Error()})
			return
		}
		managementEvents.publish("updates")
		c.JSON(http.StatusOK, gin.H{"update": record, "sent": sent, "offline": offline, "pending": false})
	})
	// HTTP package download removed: clients open a dedicated TLS session on -tls-addr
	// and use UpdateDownloadReq/Resp with the download_token from UPDATE_AVAILABLE.
	router.GET(updateDownloadPathPrefix+":sha256", func(c *gin.Context) {
		c.JSON(http.StatusGone, gin.H{
			"error": "HTTP update download removed; use TLS UpdateDownloadReq on the client port",
		})
	})
	router.GET("/api/v1/updates", func(c *gin.Context) {
		if preferences == nil {
			c.JSON(http.StatusOK, gin.H{"items": []updateRecord{}})
			return
		}
		items, err := preferences.updateRecords()
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
		c.JSON(http.StatusOK, gin.H{"items": items})
	})
	router.POST("/api/v1/updates/:update_id/withdraw", func(c *gin.Context) {
		if preferences == nil {
			c.JSON(http.StatusNotImplemented, gin.H{"error": "update persistence unavailable"})
			return
		}
		if err := preferences.withdrawUpdate(c.Param("update_id")); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		managementEvents.publish("updates")
		c.Status(http.StatusNoContent)
	})
	router.POST("/api/v1/devices", func(c *gin.Context) {
		var input approveDeviceRequest
		if err := c.ShouldBindJSON(&input); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		if err := session.ValidateClientID(input.ClientID); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		if input.ForcedMode != nil && *input.ForcedMode != "" && *input.ForcedMode != "auto" && *input.ForcedMode != "pull" && *input.ForcedMode != "listen" {
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid forced connection mode"})
			return
		}
		device, err := registry.ApproveClientID(input.ClientID, input.Label, time.Now())
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		if input.ForcedMode != nil {
			if err := registry.SetForcedMode(input.ClientID, *input.ForcedMode); err != nil {
				c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
				return
			}
			device, _ = registry.Get(input.ClientID)
		}
		if store.AddClientToBroadcasts(device.ClientID, time.Now()) > 0 {
			managementEvents.publish("messages")
		}
		managementEvents.publish("devices")
		c.JSON(http.StatusCreated, device)
	})
	router.PATCH("/api/v1/devices/:client_id", func(c *gin.Context) {
		var input renameDeviceRequest
		if err := c.ShouldBindJSON(&input); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		device, err := registry.RenameClientID(c.Param("client_id"), input.Label)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		if input.ForcedMode != nil {
			if err := registry.SetForcedMode(c.Param("client_id"), *input.ForcedMode); err != nil {
				c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
				return
			}
			device, _ = registry.Get(c.Param("client_id"))
		}
		managementEvents.publish("devices")
		c.JSON(http.StatusOK, device)
	})
	router.DELETE("/api/v1/devices/:client_id", func(c *gin.Context) {
		clientID := c.Param("client_id")
		if err := registry.RevokeClientID(clientID); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		if normalized, normErr := session.NormalizeClientID(clientID); normErr == nil {
			registry.RecordIntentionalSessionEnd(normalized, session.SessionEndAdmin, "revoked", time.Now())
		}
		hub.Disconnect(clientID)
		managementEvents.publish("devices")
		c.Status(http.StatusNoContent)
	})
	router.POST("/api/v1/devices/:client_id/disconnect", func(c *gin.Context) {
		clientID, err := session.NormalizeClientID(c.Param("client_id"))
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		registry.RecordIntentionalSessionEnd(clientID, session.SessionEndAdmin, "admin_disconnect", time.Now())
		hub.SuspendClient(clientID)
		managementEvents.publish("devices")
		c.Status(http.StatusNoContent)
	})
	router.GET("/api/v1/devices/:client_id/logs", func(c *gin.Context) {
		clientID, err := session.NormalizeClientID(c.Param("client_id"))
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		page := 1
		if value, err := strconv.Atoi(c.Query("page")); err == nil && value > 0 {
			page = value
		}
		pageSize := 100
		if value, err := strconv.Atoi(c.Query("page_size")); err == nil && value > 0 {
			pageSize = value
		}
		if !validPageSize(pageSize) {
			c.JSON(http.StatusBadRequest, gin.H{"error": "log page size must be one of 10, 15, 20, 25, 30, 50, 100 or 200"})
			return
		}
		content, total := readClientLogPage(clientLogs.snapshot(clientID), page, pageSize)
		c.JSON(http.StatusOK, gin.H{"content": content, "client_id": clientID, "page": page, "page_size": pageSize, "total": total})
	})
	router.POST("/api/v1/messages", func(c *gin.Context) {
		var input createMessageRequest
		if err := c.ShouldBindJSON(&input); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		var id broadcast.MessageID
		var err error
		if input.MessageID == "" {
			id, err = broadcast.NewMessageID()
		} else {
			id, err = broadcast.ParseMessageID(input.MessageID)
		}
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		priority := broadcast.DefaultPriority
		if input.Priority != nil {
			priority = *input.Priority
		}
		now := time.Now()
		if input.DisplayDurationRatio != nil && (*input.DisplayDurationRatio < 0 || *input.DisplayDurationRatio > 120) {
			c.JSON(http.StatusBadRequest, gin.H{"error": "display duration ratio must be between 0 and 120"})
			return
		}
		if input.DisplayPosition != nil && !config.ValidDisplayPosition(*input.DisplayPosition) {
			c.JSON(http.StatusBadRequest, gin.H{"error": "display position is invalid"})
			return
		}
		currentConfig := configManager.Current()
		if err := broadcast.ValidateSpeech(input.Speech, currentConfig.MaxSpeechDepth, currentConfig.MaxRepeatExpansion); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		// 将创建时的默认值落入消息记录，历史列表不会因后续默认配置变更而漂移；
		// AsDefault 仍让客户端在真正投递时使用连接快照中的最新默认值。
		displayPosition := input.DisplayPosition
		if displayPosition == nil {
			displayPosition = new(string)
			*displayPosition = currentConfig.DisplayPosition
		}
		displayRatio := input.DisplayDurationRatio
		if displayRatio == nil {
			displayRatio = new(float64)
			*displayRatio = currentConfig.DisplayDurationRatio
		}
		message := broadcast.Message{
			ID: id, Priority: priority, PrioritySet: input.Priority != nil,
			DisplayText: input.Content, Speech: input.Speech,
			DisplayPosition: displayPosition, DisplayDurationRatio: displayRatio,
			AsDefault: input.DisplayPosition == nil && input.DisplayDurationRatio == nil,
		}
		if input.ExpiresAt != nil {
			message.ExpiresAt = input.ExpiresAt.UnixMilli()
		} else {
			message.ExpiresAt = now.Add(currentConfig.MessageTTL).UnixMilli()
		}
		targetClientIDs := make([]string, 0, len(input.TargetClientIDs))
		for _, value := range input.TargetClientIDs {
			normalized, normalizeErr := session.NormalizeClientID(value)
			if normalizeErr != nil {
				// Keep the store/API compatible with non-fingerprint identifiers used
				// by integrations; fingerprint targets are normalized canonically.
				targetClientIDs = append(targetClientIDs, strings.TrimSpace(value))
				continue
			}
			targetClientIDs = append(targetClientIDs, normalized)
		}
		// “全部客户端”在创建时固化为已授权客户端集合，离线客户端上线后仍能补收。
		if len(targetClientIDs) == 0 {
			targetClientIDs = registry.ApprovedClientIDs()
		}
		if len(input.TargetClientIDs) == 0 && len(targetClientIDs) == 0 {
			// Keep an internal wildcard target. It will be expanded when an
			// approved client becomes available, including after this request.
			targetClientIDs = []string{"*"}
		}
		record, err := store.Create(message, targetClientIDs, now)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		managementEvents.publish("messages")
		c.JSON(http.StatusCreated, toMessageResponse(record))
	})
	router.GET("/api/v1/messages", func(c *gin.Context) {
		items := store.List(time.Now())
		response := make([]messageResponse, 0, len(items))
		for _, item := range items {
			response = append(response, toMessageResponse(item))
		}
		c.JSON(http.StatusOK, gin.H{"items": response, "warnings": store.Warnings()})
	})
	router.POST("/api/v1/messages/:message_id/withdraw", func(c *gin.Context) {
		messageID, err := broadcast.ParseMessageID(c.Param("message_id"))
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		record, ok := store.Withdraw(messageID, time.Now())
		if !ok {
			c.JSON(http.StatusConflict, gin.H{"error": "message cannot be withdrawn"})
			return
		}
		notifyWithdrawal(record, hub)
		managementEvents.publish("messages")
		c.JSON(http.StatusOK, toMessageResponse(record))
	})
	return router
}

func toConfigResponse(snapshot config.Snapshot) configResponse {
	return configResponse{
		ConfigID: snapshot.ID, IssuedAt: snapshot.IssuedAt,
		HeartbeatInterval: int64(snapshot.HeartbeatInterval / time.Second),
		HeartbeatTimeout:  int64(snapshot.HeartbeatTimeout / time.Second),
		MessageTTL:        int64(snapshot.MessageTTL / time.Hour), MaxSpeechDepth: snapshot.MaxSpeechDepth,
		AckTimeout:         int64(snapshot.AckTimeout / time.Second),
		MaxRepeatExpansion: snapshot.MaxRepeatExpansion, DisplayPosition: snapshot.DisplayPosition,
		DisplayDurationRatio:  snapshot.DisplayDurationRatio,
		ListenerProbeInterval: int64(snapshot.ListenerProbeInterval / time.Second),
		ListenerProbeDuration: int64(snapshot.ListenerProbeDuration / time.Hour),
		ListenerProbeReset:    int64(snapshot.ListenerProbeReset / (24 * time.Hour)),
		ListenerLossThreshold: snapshot.ListenerLossThreshold,
		ListenerIdleTimeout:   int64(snapshot.ListenerIdleTimeout / time.Second),
	}
}

type logPageFilter struct {
	Start          string
	End            string
	MinimumLevel   string
	SelectedLevels string
}

// readLogPageFromLines 在服务端内存中筛选并分页最近日志。
func readLogPageFromLines(lines []string, page, pageSize int, filter logPageFilter) (string, int, error) {
	selected := make(map[string]struct{})
	for _, value := range strings.Split(filter.SelectedLevels, ",") {
		if value = strings.ToUpper(strings.TrimSpace(value)); value != "" {
			selected[value] = struct{}{}
		}
	}
	minimum := logLevelRank(filter.MinimumLevel)
	start := logDateBound(filter.Start, false)
	end := logDateBound(filter.End, true)
	matched := make([]string, 0, len(lines))
	for _, line := range lines {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		var entry map[string]any
		if json.Unmarshal([]byte(line), &entry) != nil {
			if minimum > 0 || len(selected) > 0 || !start.IsZero() || !end.IsZero() {
				continue
			}
			matched = append(matched, line)
			continue
		}
		level := strings.ToUpper(strings.TrimSpace(fmt.Sprint(entry["level"])))
		if minimum >= 0 && logLevelRank(level) < minimum {
			continue
		}
		if len(selected) > 0 {
			if _, ok := selected[level]; !ok {
				continue
			}
		}
		if timestamp, ok := entry["time"].(string); ok {
			parsed, parseErr := time.Parse(time.RFC3339Nano, timestamp)
			if parseErr == nil && ((!start.IsZero() && parsed.Before(start)) || (!end.IsZero() && !parsed.Before(end))) {
				continue
			}
		}
		matched = append(matched, line)
	}
	// 日志表按最新记录在前展示，页码 1 也始终代表最新一页。
	for left, right := 0, len(matched)-1; left < right; left, right = left+1, right-1 {
		matched[left], matched[right] = matched[right], matched[left]
	}
	total := len(matched)
	if page < 1 {
		page = 1
	}
	startIndex := (page - 1) * pageSize
	if startIndex >= total {
		return "", total, nil
	}
	endIndex := startIndex + pageSize
	if endIndex > total {
		endIndex = total
	}
	return strings.Join(matched[startIndex:endIndex], "\n"), total, nil
}

func logLevelRank(value string) int {
	switch strings.ToUpper(strings.TrimSpace(value)) {
	case "DEBUG":
		return 0
	case "INFO":
		return 1
	case "WARN", "WARNING":
		return 2
	case "ERROR":
		return 3
	default:
		return -1
	}
}

func logDateBound(value string, end bool) time.Time {
	if value == "" {
		return time.Time{}
	}
	location, err := time.LoadLocation("Asia/Taipei")
	if err != nil {
		location = time.FixedZone("Asia/Taipei", 8*60*60)
	}
	date, err := time.ParseInLocation("2006-01-02", value, location)
	if err != nil {
		return time.Time{}
	}
	if end {
		return date.AddDate(0, 0, 1)
	}
	return date
}

func toMessageResponse(record broadcast.Record) messageResponse {
	targets := append([]string(nil), record.TargetClients...)
	if len(targets) == 1 && targets[0] == "*" {
		targets = nil
	}
	return messageResponse{
		MessageID: record.Message.ID.String(), QueueSeq: record.Message.QueueSeq,
		Priority: record.Message.Priority, Content: record.Message.DisplayText,
		TargetClientIDs: targets, Status: record.Status,
		CreatedAt: record.CreatedAt.UnixMilli(), ExpiresAt: record.ExpiresAt.UnixMilli(),
		DisplayPosition:      record.Message.DisplayPosition,
		DisplayDurationRatio: record.Message.DisplayDurationRatio,
		TTSEnabled:           len(record.Message.Speech) > 0,
		Deliveries:           cloneDeliveryStatuses(record.Deliveries),
		DeliveryHistory:      cloneDeliveryStatuses(record.DeliveryHistory),
	}
}

func cloneDeliveryStatuses(input map[string]broadcast.DeliveryStatus) map[string]broadcast.DeliveryStatus {
	if len(input) == 0 {
		return nil
	}
	output := make(map[string]broadcast.DeliveryStatus, len(input))
	for clientID, status := range input {
		output[clientID] = status
	}
	return output
}
