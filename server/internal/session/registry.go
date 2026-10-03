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
	"strings"
	"sync"
	"time"

	_ "modernc.org/sqlite"
)

type Device struct {
	ClientID   string `json:"client_id"`
	Label      string `json:"label"`
	Status     string `json:"status"`
	ApprovedAt int64  `json:"approved_at"`
	LastSeen   int64  `json:"last_seen,omitempty"`
}

type Registry struct {
	mu      sync.RWMutex
	devices map[string]Device
	path    string
	db      *sql.DB
}

func NewRegistry() *Registry { return &Registry{devices: make(map[string]Device)} }

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
			last_seen INTEGER NOT NULL DEFAULT 0
		)`); err != nil {
			_ = db.Close()
			return nil, err
		}
		rows, err := db.Query(`SELECT client_id, label, status, approved_at, last_seen FROM devices`)
		if err != nil {
			_ = db.Close()
			return nil, err
		}
		defer rows.Close()
		for rows.Next() {
			var device Device
			if err := rows.Scan(&device.ClientID, &device.Label, &device.Status, &device.ApprovedAt, &device.LastSeen); err != nil {
				return nil, err
			}
			id, err := NormalizeClientID(device.ClientID)
			if err != nil {
				continue
			}
			device.ClientID = id
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
	if _, ok := r.devices[normalized]; !ok {
		return errors.New("client is not registered")
	}
	delete(r.devices, normalized)
	r.persistLocked()
	return nil
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
	if device.Status == "approved" || device.ApprovedAt != 0 {
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
		r.devices[id] = device
		r.persistLocked()
	}
	r.mu.Unlock()
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
			if _, err := transaction.Exec(`INSERT INTO devices(client_id, label, status, approved_at, last_seen) VALUES (?, ?, ?, ?, ?)`, device.ClientID, device.Label, device.Status, device.ApprovedAt, device.LastSeen); err != nil {
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
	return items
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
