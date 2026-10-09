package session

import (
	"crypto/ed25519"
	"crypto/sha256"
	"crypto/x509"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"lovemilk-class-broadcaster/server/internal/config"
	"lovemilk-class-broadcaster/server/internal/fontsize"
	_ "modernc.org/sqlite"
)

// ListenerPolicy 控制监听能力试用窗口与服务端主动探测行为。
type ListenerPolicy struct {
	ProbeInterval time.Duration
	ProbeDuration time.Duration
	ProbeReset    time.Duration
	LossThreshold int
	IdleTimeout   time.Duration
}

func defaultListenerPolicy() ListenerPolicy {
	return ListenerPolicy{
		ProbeInterval: config.DefaultListenerProbeInterval,
		ProbeDuration: config.DefaultListenerProbeDuration,
		ProbeReset:    config.DefaultListenerProbeReset,
		LossThreshold: config.DefaultListenerLossThreshold,
		IdleTimeout:   config.DefaultListenerIdleTimeout,
	}
}

// Session end / offline classification shown in the admin device list.
const (
	SessionEndNone       = ""
	SessionEndUserExit   = "user_exit"   // developer panel Exit → 人为终止
	SessionEndUpdate     = "update"      // client handed off to updater
	SessionEndAdmin      = "admin"       // admin disconnect / revoke
	SessionEndUnexpected = "unexpected" // dropped without goodbye; confirmed after grace
)

// IsIntentionalSessionEnd reports goodbye reasons that must not count as listen-probe loss.
func IsIntentionalSessionEnd(reason string) bool {
	switch reason {
	case SessionEndUserExit, SessionEndUpdate, SessionEndAdmin:
		return true
	default:
		return false
	}
}

// UnexpectedDisconnectGrace is how long an unexplained drop stays "reconnecting"
// before the admin UI marks it as 意外终止.
const UnexpectedDisconnectGrace = 30 * time.Second

type Device struct {
	ClientID          string `json:"client_id"`
	ClientVersion     string `json:"client_version,omitempty"`
	// ClientBuildDate mirrors the client's stamped build time for admin display
	// (same format as the server health build_date). Not used for update comparisons.
	ClientBuildDate   string `json:"client_build_date,omitempty"`
	Label             string `json:"label"`
	Status            string `json:"status"`
	ApprovedAt        int64  `json:"approved_at"`
	LastSeen          int64  `json:"last_seen,omitempty"`
	ConnectionMode    string `json:"connection_mode,omitempty"`
	RequestedMode     string `json:"requested_mode,omitempty"`
	ForcedMode        string `json:"forced_mode,omitempty"`
	ListenerPort      int    `json:"listener_port,omitempty"`
	ListenerAddress   string `json:"listener_address,omitempty"`
	ProbeStartedAt    int64  `json:"probe_started_at,omitempty"`
	ProbeUntil        int64  `json:"probe_until,omitempty"`
	ProbeSent         int64  `json:"probe_sent,omitempty"`
	ProbeReceived     int64  `json:"probe_received,omitempty"`
	ProbeMinLatencyMS int64  `json:"probe_min_latency_ms,omitempty"`
	ProbeMaxLatencyMS int64  `json:"probe_max_latency_ms,omitempty"`
	ProbeAvgLatencyMS int64  `json:"probe_avg_latency_ms,omitempty"`
	ProbeLossPercent  int    `json:"probe_loss_percent,omitempty"`
	ProbeResetAt      int64  `json:"probe_reset_at,omitempty"`
	// SessionEndReason is the last known offline cause (user_exit / update / admin / unexpected).
	SessionEndReason string `json:"session_end_reason,omitempty"`
	// SessionEndAt is unix ms when the session ended or the unexpected drop was observed.
	SessionEndAt int64 `json:"session_end_at,omitempty"`
	// SessionEndDetail is an optional short note (e.g. update sha, admin action).
	SessionEndDetail string `json:"session_end_detail,omitempty"`
	// AvailableFonts is the last font inventory reported at handshake (JSON array in SQLite).
	AvailableFonts []string `json:"available_fonts,omitempty"`
	// PlatformDefaultFont is the client's resolved platform default at last handshake.
	PlatformDefaultFont string `json:"platform_default_font,omitempty"`
	// DefaultFontFamily is an admin override pushed on ConnectResp (empty = platform default).
	DefaultFontFamily string `json:"default_font_family,omitempty"`
	// DefaultFontSize is an admin expression pushed on ConnectResp (empty = client built-in 25vh).
	// Examples: "38px", "1.2em", "5vw", "25vh".
	DefaultFontSize string `json:"default_font_size,omitempty"`
}

type Registry struct {
	mu      sync.RWMutex
	devices map[string]Device
	path    string
	db      *sql.DB
	policy  ListenerPolicy
}

func NewRegistry() *Registry {
	return &Registry{devices: make(map[string]Device), policy: defaultListenerPolicy()}
}

// SetListenerPolicy 更新监听探测策略；正在进行的试用窗口使用新阈值完成判定。
func (r *Registry) SetListenerPolicy(policy ListenerPolicy) {
	if policy.ProbeInterval <= 0 {
		policy.ProbeInterval = config.DefaultListenerProbeInterval
	}
	if policy.ProbeDuration <= 0 {
		policy.ProbeDuration = config.DefaultListenerProbeDuration
	}
	if policy.ProbeReset <= 0 {
		policy.ProbeReset = config.DefaultListenerProbeReset
	}
	if policy.LossThreshold < 0 || policy.LossThreshold > 100 {
		policy.LossThreshold = config.DefaultListenerLossThreshold
	}
	if policy.IdleTimeout <= 0 {
		policy.IdleTimeout = config.DefaultListenerIdleTimeout
	}
	r.mu.Lock()
	r.policy = policy
	r.mu.Unlock()
}

// ListenerPolicy 返回当前监听探测策略的副本。
func (r *Registry) ListenerPolicy() ListenerPolicy {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.policy
}

func NewPersistentRegistry(path string) (*Registry, error) {
	r := NewRegistry()
	r.path = path
	if filepath.Ext(path) == ".db" {
		db, err := sql.Open("sqlite", path)
		if err != nil {
			return nil, err
		}
		r.db = db
		if _, err := db.Exec(`CREATE TABLE IF NOT EXISTS devices (
			client_id TEXT PRIMARY KEY,
			label TEXT NOT NULL DEFAULT '',
			status TEXT NOT NULL,
			approved_at INTEGER NOT NULL DEFAULT 0,
			last_seen INTEGER NOT NULL DEFAULT 0,
			connection_mode TEXT NOT NULL DEFAULT 'pull',
			requested_mode TEXT NOT NULL DEFAULT 'auto',
			listener_port INTEGER NOT NULL DEFAULT 0,
			listener_address TEXT NOT NULL DEFAULT '',
			probe_started_at INTEGER NOT NULL DEFAULT 0,
			probe_until INTEGER NOT NULL DEFAULT 0,
			probe_sent INTEGER NOT NULL DEFAULT 0,
			probe_received INTEGER NOT NULL DEFAULT 0,
			probe_min_latency_ms INTEGER NOT NULL DEFAULT 0,
			probe_max_latency_ms INTEGER NOT NULL DEFAULT 0,
			probe_avg_latency_ms INTEGER NOT NULL DEFAULT 0,
			probe_loss_percent INTEGER NOT NULL DEFAULT 0
		)`); err != nil {
			_ = db.Close()
			return nil, err
		}
		for _, column := range []string{
			"connection_mode TEXT NOT NULL DEFAULT 'pull'", "requested_mode TEXT NOT NULL DEFAULT 'auto'", "forced_mode TEXT NOT NULL DEFAULT ''",
			"client_version TEXT NOT NULL DEFAULT ''",
			"client_build_date TEXT NOT NULL DEFAULT ''",
			"listener_port INTEGER NOT NULL DEFAULT 0", "listener_address TEXT NOT NULL DEFAULT ''",
			"probe_started_at INTEGER NOT NULL DEFAULT 0", "probe_until INTEGER NOT NULL DEFAULT 0",
			"probe_sent INTEGER NOT NULL DEFAULT 0", "probe_received INTEGER NOT NULL DEFAULT 0",
			"probe_min_latency_ms INTEGER NOT NULL DEFAULT 0", "probe_max_latency_ms INTEGER NOT NULL DEFAULT 0",
			"probe_avg_latency_ms INTEGER NOT NULL DEFAULT 0", "probe_loss_percent INTEGER NOT NULL DEFAULT 0",
			"probe_reset_at INTEGER NOT NULL DEFAULT 0",
			"session_end_reason TEXT NOT NULL DEFAULT ''",
			"session_end_at INTEGER NOT NULL DEFAULT 0",
			"session_end_detail TEXT NOT NULL DEFAULT ''",
			"available_fonts TEXT NOT NULL DEFAULT '[]'",
			"platform_default_font TEXT NOT NULL DEFAULT ''",
			"default_font_family TEXT NOT NULL DEFAULT ''",
			"default_font_size TEXT NOT NULL DEFAULT ''",
		} {
			_, _ = db.Exec(`ALTER TABLE devices ADD COLUMN ` + column)
		}
		rows, err := db.Query(`SELECT client_id, label, status, approved_at, last_seen, connection_mode, requested_mode, forced_mode, client_version, COALESCE(client_build_date,''), listener_port, listener_address, probe_started_at, probe_until, probe_sent, probe_received, probe_min_latency_ms, probe_max_latency_ms, probe_avg_latency_ms, probe_loss_percent, probe_reset_at, session_end_reason, session_end_at, session_end_detail, COALESCE(available_fonts,'[]'), COALESCE(platform_default_font,''), COALESCE(default_font_family,''), COALESCE(CAST(default_font_size AS TEXT),'') FROM devices`)
		if err != nil {
			_ = db.Close()
			return nil, err
		}
		defer rows.Close()
		for rows.Next() {
			var device Device
			var fontsJSON string
			var fontSizeRaw string
			if err := rows.Scan(&device.ClientID, &device.Label, &device.Status, &device.ApprovedAt, &device.LastSeen, &device.ConnectionMode, &device.RequestedMode, &device.ForcedMode, &device.ClientVersion, &device.ClientBuildDate, &device.ListenerPort, &device.ListenerAddress, &device.ProbeStartedAt, &device.ProbeUntil, &device.ProbeSent, &device.ProbeReceived, &device.ProbeMinLatencyMS, &device.ProbeMaxLatencyMS, &device.ProbeAvgLatencyMS, &device.ProbeLossPercent, &device.ProbeResetAt, &device.SessionEndReason, &device.SessionEndAt, &device.SessionEndDetail, &fontsJSON, &device.PlatformDefaultFont, &device.DefaultFontFamily, &fontSizeRaw); err != nil {
				return nil, err
			}
			id, err := NormalizeClientID(device.ClientID)
			if err != nil {
				continue
			}
			device.ClientID = id
			device.AvailableFonts = decodeFontList(fontsJSON)
			if normalized, normErr := fontsize.Normalize(migrateLegacyFontSize(fontSizeRaw)); normErr == nil {
				device.DefaultFontSize = normalized
			}
			r.devices[id] = device
		}
		if err := rows.Err(); err != nil {
			return nil, err
		}
		_ = rows.Close()
		return r, nil
	}
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return r, nil
	}
	if err != nil {
		return nil, err
	}
	if len(data) == 0 {
		return r, nil
	}
	var devices []Device
	if err := json.Unmarshal(data, &devices); err != nil {
		return nil, err
	}
	for _, device := range devices {
		id, err := NormalizeClientID(device.ClientID)
		if err != nil {
			continue
		}
		device.ClientID = id
		if device.Status == "" {
			device.Status = "approved"
		}
		r.devices[id] = device
	}
	return r, nil
}

func ClientID(publicKey []byte) (string, error) {
	if len(publicKey) != ed25519.PublicKeySize {
		return "", errors.New("client public key must be 32 bytes")
	}
	spki, err := x509.MarshalPKIXPublicKey(ed25519.PublicKey(publicKey))
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(spki)
	return hex.EncodeToString(digest[:]), nil
}

func ValidateClientID(value string) error {
	_, err := NormalizeClientID(value)
	return err
}

func NormalizeClientID(value string) (string, error) {
	value = strings.TrimSpace(value)
	if len(value) >= len("SHA256:") && strings.EqualFold(value[:len("SHA256:")], "SHA256:") {
		encoded := strings.TrimRight(value[len("SHA256:"):], "=")
		digest, err := base64.RawStdEncoding.DecodeString(encoded)
		if err != nil || len(digest) != sha256.Size {
			return "", errors.New("client id must be a SHA-256 fingerprint")
		}
		return hex.EncodeToString(digest), nil
	}
	digest, err := hex.DecodeString(value)
	if err != nil || len(digest) != sha256.Size {
		return "", errors.New("client id must be a 64-character SHA-256 fingerprint hex or SHA256 base64")
	}
	return hex.EncodeToString(digest), nil
}

func (r *Registry) Approve(publicKey []byte, label string, now time.Time) (Device, error) {
	id, err := ClientID(publicKey)
	if err != nil {
		return Device{}, err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	device := r.devices[id]
	device.ClientID = id
	device.Label = label
	device.Status = "approved"
	if device.ApprovedAt == 0 {
		device.ApprovedAt = now.UnixMilli()
	}
	r.devices[id] = device
	r.persistLocked()
	return device, nil
}

func (r *Registry) ApproveClientID(id, label string, now time.Time) (Device, error) {
	normalized, err := NormalizeClientID(id)
	if err != nil {
		return Device{}, err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	device := r.devices[normalized]
	device.ClientID = normalized
	device.Label = label
	device.Status = "approved"
	if device.ApprovedAt == 0 {
		device.ApprovedAt = now.UnixMilli()
	}
	r.devices[normalized] = device
	r.persistLocked()
	return device, nil
}

func (r *Registry) RenameClientID(id, label string) (Device, error) {
	normalized, err := NormalizeClientID(id)
	if err != nil {
		return Device{}, err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	device, ok := r.devices[normalized]
	if !ok {
		return Device{}, errors.New("client is not registered")
	}
	device.Label = strings.TrimSpace(label)
	r.devices[normalized] = device
	r.persistLocked()
	return device, nil
}

func (r *Registry) RevokeClientID(id string) error {
	normalized, err := NormalizeClientID(id)
	if err != nil {
		return err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	device, ok := r.devices[normalized]
	if !ok {
		return errors.New("client is not registered")
	}
	device.Status = "revoked"
	r.devices[normalized] = device
	r.persistLocked()
	return nil
}

// DeviceRetention is the maximum age for idle client device records.
const DeviceRetention = 185 * 24 * time.Hour

// PurgeStaleDevices removes client device records that have not been seen
// (and were not approved more recently) within maxAge. Online activity is
// reflected by LastSeen; ApprovedAt is used when LastSeen is zero.
func (r *Registry) PurgeStaleDevices(now time.Time, maxAge time.Duration) int {
	if maxAge <= 0 {
		maxAge = DeviceRetention
	}
	cutoffMS := now.Add(-maxAge).UnixMilli()
	r.mu.Lock()
	defer r.mu.Unlock()
	removed := 0
	for id, device := range r.devices {
		reference := device.LastSeen
		if reference <= 0 {
			reference = device.ApprovedAt
		}
		// Never drop a brand-new pending sighting with no timestamps yet
		// within the same second; require a positive reference that is old.
		if reference <= 0 || reference > cutoffMS {
			continue
		}
		delete(r.devices, id)
		removed++
	}
	if removed > 0 {
		r.persistLocked()
	}
	return removed
}

func (r *Registry) IsApproved(publicKey []byte) bool {
	id, err := ClientID(publicKey)
	if err != nil {
		return false
	}
	r.mu.RLock()
	device, ok := r.devices[id]
	r.mu.RUnlock()
	return ok && (device.Status == "" || device.Status == "approved")
}

// ObservePending records a client that completed TLS but is not trusted yet.
func (r *Registry) ObservePending(publicKey []byte, now time.Time) (Device, error) {
	id, err := ClientID(publicKey)
	if err != nil {
		return Device{}, err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	device := r.devices[id]
	device.ClientID = id
	if device.Status == "approved" || device.Status == "revoked" || device.ApprovedAt != 0 {
		device.LastSeen = now.UnixMilli()
		r.devices[id] = device
		r.persistLocked()
		return device, nil
	}
	device.Status = "pending"
	device.LastSeen = now.UnixMilli()
	r.devices[id] = device
	r.persistLocked()
	return device, nil
}

func (r *Registry) MarkSeen(publicKey []byte, now time.Time) {
	id, err := ClientID(publicKey)
	if err != nil {
		return
	}
	r.mu.Lock()
	device, ok := r.devices[id]
	if ok {
		device.LastSeen = now.UnixMilli()
		// Live session clears any previous offline classification.
		device.SessionEndReason = SessionEndNone
		device.SessionEndAt = 0
		device.SessionEndDetail = ""
		r.devices[id] = device
		r.persistLocked()
	}
	r.mu.Unlock()
}

// ClearSessionEnd marks a client as live again (called on successful hub registration).
func (r *Registry) ClearSessionEnd(clientID string, now time.Time) {
	id, err := NormalizeClientID(clientID)
	if err != nil {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	device, ok := r.devices[id]
	if !ok {
		return
	}
	device.LastSeen = now.UnixMilli()
	device.SessionEndReason = SessionEndNone
	device.SessionEndAt = 0
	device.SessionEndDetail = ""
	r.devices[id] = device
	r.persistLocked()
}

// RecordIntentionalSessionEnd stores a client-reported or admin-driven goodbye.
// reason: user_exit | update | admin
func (r *Registry) RecordIntentionalSessionEnd(clientID, reason, detail string, now time.Time) {
	id, err := NormalizeClientID(clientID)
	if err != nil {
		return
	}
	switch reason {
	case SessionEndUserExit, SessionEndUpdate, SessionEndAdmin:
	default:
		reason = SessionEndUserExit
	}
	if len(detail) > 256 {
		detail = detail[:256]
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	device, ok := r.devices[id]
	if !ok {
		return
	}
	device.SessionEndReason = reason
	device.SessionEndAt = now.UnixMilli()
	device.SessionEndDetail = detail
	device.LastSeen = now.UnixMilli()
	r.devices[id] = device
	r.persistLocked()
}

// NoteUnexpectedDisconnect starts the 30s grace window after an unexplained drop.
// If the client already reported an intentional end, this is a no-op.
func (r *Registry) NoteUnexpectedDisconnect(clientID string, now time.Time) {
	id, err := NormalizeClientID(clientID)
	if err != nil {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	device, ok := r.devices[id]
	if !ok {
		return
	}
	switch device.SessionEndReason {
	case SessionEndUserExit, SessionEndUpdate, SessionEndAdmin, SessionEndUnexpected:
		// Keep the intentional or already-confirmed classification.
		return
	}
	// Pending unexpected: reason empty but SessionEndAt set → grace in progress.
	device.SessionEndReason = SessionEndNone
	device.SessionEndAt = now.UnixMilli()
	device.SessionEndDetail = "awaiting_reconnect"
	device.LastSeen = now.UnixMilli()
	r.devices[id] = device
	r.persistLocked()
}

// FinalizeUnexpectedDisconnects promotes grace-elapsed drops to 意外终止.
// onlineIDs must be the currently connected hub set (those are skipped).
// Returns how many devices newly became unexpected.
func (r *Registry) FinalizeUnexpectedDisconnects(onlineIDs map[string]struct{}, now time.Time) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	changed := 0
	graceMS := UnexpectedDisconnectGrace.Milliseconds()
	nowMS := now.UnixMilli()
	for id, device := range r.devices {
		if _, online := onlineIDs[id]; online {
			continue
		}
		// Already classified.
		if device.SessionEndReason == SessionEndUserExit ||
			device.SessionEndReason == SessionEndUpdate ||
			device.SessionEndReason == SessionEndAdmin ||
			device.SessionEndReason == SessionEndUnexpected {
			continue
		}
		// Grace pending: empty reason + SessionEndAt set by NoteUnexpectedDisconnect.
		if device.SessionEndAt <= 0 {
			continue
		}
		if nowMS-device.SessionEndAt < graceMS {
			continue
		}
		device.SessionEndReason = SessionEndUnexpected
		if device.SessionEndDetail == "awaiting_reconnect" || device.SessionEndDetail == "" {
			device.SessionEndDetail = "no_reconnect_within_30s"
		}
		r.devices[id] = device
		changed++
	}
	if changed > 0 {
		r.persistLocked()
	}
	return changed
}

// SessionEndDisplay returns the admin-facing offline label for a device.
// online=true → empty (caller shows 已连接).
func SessionEndDisplay(device Device, online bool, now time.Time) (reason string, label string) {
	if online {
		return "", ""
	}
	switch device.SessionEndReason {
	case SessionEndUserExit:
		return SessionEndUserExit, "人为终止"
	case SessionEndUpdate:
		return SessionEndUpdate, "更新重启中"
	case SessionEndAdmin:
		return SessionEndAdmin, "管理端断开"
	case SessionEndUnexpected:
		return SessionEndUnexpected, "意外终止"
	}
	// Grace window after unexplained drop.
	if device.SessionEndAt > 0 && device.SessionEndDetail == "awaiting_reconnect" {
		elapsed := now.UnixMilli() - device.SessionEndAt
		if elapsed < UnexpectedDisconnectGrace.Milliseconds() {
			return "reconnecting", "重连中"
		}
		return SessionEndUnexpected, "意外终止"
	}
	return "", "未连接"
}

// UpdateClientVersion stores the version reported by an authenticated client handshake.
// buildDate is optional (empty on older clients) and is display-only.
func (r *Registry) UpdateClientVersion(publicKey []byte, version string, buildDate string) {
	id, err := ClientID(publicKey)
	if err != nil || len(version) > 64 {
		return
	}
	buildDate = strings.TrimSpace(buildDate)
	if len(buildDate) > 64 {
		buildDate = buildDate[:64]
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	device, ok := r.devices[id]
	if !ok {
		return
	}
	device.ClientVersion = version
	if buildDate != "" {
		device.ClientBuildDate = buildDate
	}
	r.devices[id] = device
	r.persistLocked()
}

func decodeFontList(raw string) []string {
	raw = strings.TrimSpace(raw)
	if raw == "" || raw == "[]" {
		return nil
	}
	var fonts []string
	if err := json.Unmarshal([]byte(raw), &fonts); err != nil {
		return nil
	}
	return normalizeFontList(fonts)
}

func encodeFontList(fonts []string) string {
	normalized := normalizeFontList(fonts)
	if len(normalized) == 0 {
		return "[]"
	}
	data, err := json.Marshal(normalized)
	if err != nil {
		return "[]"
	}
	return string(data)
}

func normalizeFontList(fonts []string) []string {
	seen := make(map[string]struct{}, len(fonts))
	out := make([]string, 0, len(fonts))
	for _, item := range fonts {
		name := strings.TrimSpace(item)
		if name == "" || len(name) > 128 {
			continue
		}
		key := strings.ToLower(name)
		if _, ok := seen[key]; ok {
			continue
		}
		seen[key] = struct{}{}
		out = append(out, name)
		if len(out) >= 200 {
			break
		}
	}
	return out
}

// UpdateClientFonts stores handshake-reported font inventory and platform default.
func (r *Registry) UpdateClientFonts(publicKey []byte, fonts []string, platformDefault string) {
	id, err := ClientID(publicKey)
	if err != nil {
		return
	}
	platformDefault = strings.TrimSpace(platformDefault)
	if len(platformDefault) > 128 {
		platformDefault = platformDefault[:128]
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	device, ok := r.devices[id]
	if !ok {
		return
	}
	// Older clients omit available_fonts; do not wipe a previously reported inventory.
	if normalized := normalizeFontList(fonts); len(normalized) > 0 {
		device.AvailableFonts = normalized
	}
	if platformDefault != "" {
		device.PlatformDefaultFont = platformDefault
	}
	r.devices[id] = device
	r.persistLocked()
}

// DefaultFontFamilyFor returns the admin per-device font override (may be empty).
func (r *Registry) DefaultFontFamilyFor(clientID string) string {
	normalized, err := NormalizeClientID(clientID)
	if err != nil {
		return ""
	}
	r.mu.RLock()
	defer r.mu.RUnlock()
	device, ok := r.devices[normalized]
	if !ok {
		return ""
	}
	return device.DefaultFontFamily
}

// DefaultFontSizeFor returns the admin per-device font size expression (empty = client default).
func (r *Registry) DefaultFontSizeFor(clientID string) string {
	normalized, err := NormalizeClientID(clientID)
	if err != nil {
		return ""
	}
	r.mu.RLock()
	defer r.mu.RUnlock()
	device, ok := r.devices[normalized]
	if !ok {
		return ""
	}
	return device.DefaultFontSize
}

// SetDefaultFontFamily sets an admin override for one client. Empty clears the override.
func (r *Registry) SetDefaultFontFamily(id, family string) error {
	normalized, err := NormalizeClientID(id)
	if err != nil {
		return err
	}
	family = strings.TrimSpace(family)
	if family == "(默认)" || strings.EqualFold(family, "(default)") || strings.EqualFold(family, "default") {
		family = ""
	}
	if len(family) > 128 {
		return errors.New("default font family is too long")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	device, ok := r.devices[normalized]
	if !ok {
		return errors.New("client is not registered")
	}
	device.DefaultFontFamily = family
	r.devices[normalized] = device
	r.persistLocked()
	return nil
}

// migrateLegacyFontSize maps old integer column values ("38") to expressions.
func migrateLegacyFontSize(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" || raw == "0" {
		return ""
	}
	return raw
}

// SetDefaultFontSize sets an admin font-size expression. Empty clears (client uses built-in 25vh).
func (r *Registry) SetDefaultFontSize(id string, size string) error {
	normalized, err := NormalizeClientID(id)
	if err != nil {
		return err
	}
	canonical, err := fontsize.Normalize(migrateLegacyFontSize(size))
	if err != nil {
		return err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	device, ok := r.devices[normalized]
	if !ok {
		return errors.New("client is not registered")
	}
	device.DefaultFontSize = canonical
	r.devices[normalized] = device
	r.persistLocked()
	return nil
}

// UpdateListenerCapability 保存客户端在握手中声明的监听能力和服务端可达地址。
func (r *Registry) UpdateListenerCapability(publicKey []byte, requestedMode string, listenerPort int, address string, now time.Time) {
	id, err := ClientID(publicKey)
	if err != nil {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	device, ok := r.devices[id]
	if !ok {
		return
	}
	if device.ForcedMode != "" {
		requestedMode = device.ForcedMode
	}
	if requestedMode != "auto" && requestedMode != "pull" && requestedMode != "listen" {
		requestedMode = "auto"
	}
	device.RequestedMode = requestedMode
	device.ListenerPort = listenerPort
	device.ListenerAddress = address
	policy := r.policy
	previousConnectionMode := device.ConnectionMode
	if requestedMode == "listen" && listenerPort > 0 {
		device.ConnectionMode = "listen"
	} else {
		device.ConnectionMode = "pull"
	}
	if requestedMode == "auto" && listenerPort > 0 && previousConnectionMode != "listen" && device.ProbeUntil == 0 && (device.ProbeResetAt == 0 || now.UnixMilli() >= device.ProbeResetAt) {
		device.ProbeStartedAt = now.UnixMilli()
		device.ProbeUntil = now.Add(policy.ProbeDuration).UnixMilli()
		device.ProbeResetAt = 0
	}
	device.LastSeen = now.UnixMilli()
	r.devices[id] = device
	r.persistLocked()
}

// SetForcedMode 由管理端指定单个客户端使用自动、从模式或监听模式。
func (r *Registry) SetForcedMode(id, mode string) error {
	normalized, err := NormalizeClientID(id)
	if err != nil {
		return err
	}
	if mode != "" && mode != "auto" && mode != "pull" && mode != "listen" {
		return errors.New("invalid forced connection mode")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	device, ok := r.devices[normalized]
	if !ok {
		return errors.New("client is not registered")
	}
	device.ForcedMode = mode
	if mode == "pull" || mode == "listen" {
		device.ConnectionMode = mode
	}
	r.devices[normalized] = device
	r.persistLocked()
	return nil
}

// RecordListenerProbe 累计主动探测结果，并在 12 小时试用窗口结束后决定模式。
func (r *Registry) RecordListenerProbe(clientID string, received bool, latency time.Duration, now time.Time) {
	normalized, err := NormalizeClientID(clientID)
	if err != nil {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	device, ok := r.devices[normalized]
	if !ok || device.RequestedMode != "auto" || device.ListenerPort == 0 {
		return
	}
	if device.ProbeUntil == 0 || (device.ProbeResetAt > 0 && now.UnixMilli() < device.ProbeResetAt) {
		return
	}
	device.ProbeSent++
	if received {
		device.ProbeReceived++
		value := latency.Milliseconds()
		if device.ProbeMinLatencyMS == 0 || value < device.ProbeMinLatencyMS {
			device.ProbeMinLatencyMS = value
		}
		if value > device.ProbeMaxLatencyMS {
			device.ProbeMaxLatencyMS = value
		}
		device.ProbeAvgLatencyMS = ((device.ProbeAvgLatencyMS * (device.ProbeReceived - 1)) + value) / device.ProbeReceived
	}
	device.ProbeLossPercent = int((device.ProbeSent - device.ProbeReceived) * 100 / maxInt64(device.ProbeSent, 1))
	policy := r.policy
	if now.UnixMilli() >= device.ProbeUntil {
		if device.ProbeLossPercent <= policy.LossThreshold {
			device.ConnectionMode = "listen"
			// 试用成功后结束探测窗口，避免每次握手重复启动同一轮试用。
			device.ProbeUntil = 0
		} else {
			device.ConnectionMode = "pull"
			device.ProbeResetAt = now.Add(policy.ProbeReset).UnixMilli()
			device.ProbeUntil = 0
		}
	}
	r.devices[normalized] = device
	r.persistLocked()
}

func maxInt64(value, fallback int64) int64 {
	if value < fallback {
		return fallback
	}
	return value
}

func (r *Registry) persistLocked() {
	if r.db != nil {
		transaction, err := r.db.Begin()
		if err != nil {
			return
		}
		if _, err := transaction.Exec(`DELETE FROM devices`); err != nil {
			_ = transaction.Rollback()
			return
		}
		for _, device := range r.devices {
			if _, err := transaction.Exec(`INSERT INTO devices(client_id, label, status, approved_at, last_seen, connection_mode, requested_mode, forced_mode, client_version, client_build_date, listener_port, listener_address, probe_started_at, probe_until, probe_sent, probe_received, probe_min_latency_ms, probe_max_latency_ms, probe_avg_latency_ms, probe_loss_percent, probe_reset_at, session_end_reason, session_end_at, session_end_detail, available_fonts, platform_default_font, default_font_family, default_font_size) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`, device.ClientID, device.Label, device.Status, device.ApprovedAt, device.LastSeen, device.ConnectionMode, device.RequestedMode, device.ForcedMode, device.ClientVersion, device.ClientBuildDate, device.ListenerPort, device.ListenerAddress, device.ProbeStartedAt, device.ProbeUntil, device.ProbeSent, device.ProbeReceived, device.ProbeMinLatencyMS, device.ProbeMaxLatencyMS, device.ProbeAvgLatencyMS, device.ProbeLossPercent, device.ProbeResetAt, device.SessionEndReason, device.SessionEndAt, device.SessionEndDetail, encodeFontList(device.AvailableFonts), device.PlatformDefaultFont, device.DefaultFontFamily, device.DefaultFontSize); err != nil {
				_ = transaction.Rollback()
				return
			}
		}
		_ = transaction.Commit()
		return
	}
	if r.path == "" {
		return
	}
	items := make([]Device, 0, len(r.devices))
	for _, device := range r.devices {
		items = append(items, device)
	}
	data, err := json.Marshal(items)
	if err != nil {
		return
	}
	if err := os.MkdirAll(filepath.Dir(r.path), 0o700); err != nil {
		return
	}
	temporary, err := os.CreateTemp(filepath.Dir(r.path), ".devices-*.tmp")
	if err != nil {
		return
	}
	temporaryName := temporary.Name()
	defer func() {
		_ = temporary.Close()
		_ = os.Remove(temporaryName)
	}()
	if _, err := temporary.Write(data); err != nil {
		return
	}
	if err := temporary.Sync(); err != nil {
		return
	}
	if err := temporary.Close(); err != nil {
		return
	}
	_ = os.Rename(temporaryName, r.path)
}

func (r *Registry) List() []Device {
	r.mu.RLock()
	defer r.mu.RUnlock()
	items := make([]Device, 0, len(r.devices))
	for _, device := range r.devices {
		items = append(items, device)
	}
	// Stable order across refreshes: map iteration is randomized in Go.
	// Pending clients first so admins can approve without scrolling; then approved,
	// then revoked. Within a group: approved_at desc + client_id (not last_seen —
	// heartbeats must not reshuffle the list on every poll/SSE).
	authRank := func(status string) int {
		switch status {
		case "pending":
			return 0
		case "approved":
			return 1
		default:
			return 2
		}
	}
	sort.SliceStable(items, func(i, j int) bool {
		ai, aj := items[i], items[j]
		ri, rj := authRank(ai.Status), authRank(aj.Status)
		if ri != rj {
			return ri < rj
		}
		if ai.ApprovedAt != aj.ApprovedAt {
			return ai.ApprovedAt > aj.ApprovedAt
		}
		return ai.ClientID < aj.ClientID
	})
	return items
}

// Get 返回单个客户端的当前授权与连接能力状态。
func (r *Registry) Get(id string) (Device, error) {
	normalized, err := NormalizeClientID(id)
	if err != nil {
		return Device{}, err
	}
	r.mu.RLock()
	defer r.mu.RUnlock()
	device, ok := r.devices[normalized]
	if !ok {
		return Device{}, errors.New("client is not registered")
	}
	return device, nil
}

// ConnectionModeFor returns the effective server-side mode used for reconnect decisions.
func (r *Registry) ConnectionModeFor(id string) string {
	device, err := r.Get(id)
	if err != nil || (device.ConnectionMode != "pull" && device.ConnectionMode != "listen") {
		return "pull"
	}
	return device.ConnectionMode
}

func (r *Registry) ClientIDs() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	ids := make([]string, 0, len(r.devices))
	for id := range r.devices {
		ids = append(ids, id)
	}
	return ids
}

// ApprovedClientIDs 返回消息创建时可接收“全部客户端”消息的授权客户端集合。
func (r *Registry) ApprovedClientIDs() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	ids := make([]string, 0, len(r.devices))
	for id, device := range r.devices {
		if device.Status == "" || device.Status == "approved" {
			ids = append(ids, id)
		}
	}
	sort.Strings(ids)
	return ids
}
