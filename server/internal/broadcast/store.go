package broadcast

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"

	_ "modernc.org/sqlite"
)

const DefaultPriority uint16 = 1024
const HistoryRetention = 48 * time.Hour

type DeliveryStatus string

const (
	DeliveryPending   DeliveryStatus = "pending"
	DeliverySent      DeliveryStatus = "sent"
	DeliveryReceived  DeliveryStatus = "received"
	DeliveryDisplayed DeliveryStatus = "displayed"
	DeliverySpoken    DeliveryStatus = "spoken"
	DeliveryFailed    DeliveryStatus = "failed"
	DeliveryExpired   DeliveryStatus = "expired"
	DeliveryWithdrawn DeliveryStatus = "withdrawn"
)

type Record struct {
	Message       Message
	TargetClients []string                  `json:"target_client_ids"`
	Status        DeliveryStatus            `json:"status"`
	CreatedAt     time.Time                 `json:"created_at"`
	ExpiresAt     time.Time                 `json:"expires_at"`
	Deliveries    map[string]DeliveryStatus `json:"deliveries,omitempty"`
}

type Store struct {
	mu       sync.RWMutex
	records  map[MessageID]Record
	queue    *Queue
	warnings []string
	path     string
	db       *sql.DB
}

func NewStore() *Store {
	return &Store{records: make(map[MessageID]Record), queue: NewQueue()}
}

// NewPersistentStore restores the SQLite snapshot payload and requeues all
// messages that were still pending when the previous process stopped.
func NewPersistentStore(path string) (*Store, error) {
	s := NewStore()
	s.path = path
	var data []byte
	var err error
	if filepath.Ext(path) == ".db" {
		s.db, err = sql.Open("sqlite", path)
		if err == nil {
			err = s.db.Ping()
		}
		if err == nil {
			_, err = s.db.Exec(`CREATE TABLE IF NOT EXISTS store_snapshots (id INTEGER PRIMARY KEY CHECK (id = 1), payload BLOB NOT NULL, updated_at INTEGER NOT NULL)`)
		}
		if err == nil {
			_ = s.db.QueryRow(`SELECT payload FROM store_snapshots WHERE id = 1`).Scan(&data)
		}
	} else {
		data, err = os.ReadFile(path)
	}
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("read message store: %w", err)
	}
	if len(data) == 0 {
		return s, nil
	}
	var snapshot struct {
		Records  []Record `json:"records"`
		Warnings []string `json:"warnings"`
	}
	if err := json.Unmarshal(data, &snapshot); err != nil {
		return nil, fmt.Errorf("decode message store: %w", err)
	}
	s.warnings = snapshot.Warnings
	for _, record := range snapshot.Records {
		if record.Message.ID.IsZero() {
			return nil, errors.New("message store contains a zero message ID")
		}
		s.records[record.Message.ID] = record
		if record.Status == DeliveryPending {
			if _, err := s.queue.EnqueueWithSequence(record.Message); err != nil {
				return nil, fmt.Errorf("restore pending message: %w", err)
			}
		}
	}
	return s, nil
}

func (s *Store) pruneLocked(now time.Time) int {
	cutoff := now.Add(-HistoryRetention)
	removed := 0
	for id, record := range s.records {
		if !record.CreatedAt.After(cutoff) {
			delete(s.records, id)
			removed++
		}
	}
	return removed
}

func (s *Store) persistLocked() {
	if s.path == "" && s.db == nil {
		return
	}
	snapshot := struct {
		Records  []Record `json:"records"`
		Warnings []string `json:"warnings"`
	}{Records: make([]Record, 0, len(s.records)), Warnings: append([]string(nil), s.warnings...)}
	for _, record := range s.records {
		snapshot.Records = append(snapshot.Records, record)
	}
	sort.Slice(snapshot.Records, func(i, j int) bool {
		return snapshot.Records[i].Message.QueueSeq < snapshot.Records[j].Message.QueueSeq
	})
	data, err := json.Marshal(snapshot)
	if s.db != nil {
		if err == nil {
			_, err = s.db.Exec(`INSERT INTO store_snapshots (id, payload, updated_at) VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at`, data, time.Now().UnixMilli())
		}
		if err != nil {
			warning := "message store persistence failed: " + err.Error()
			if len(s.warnings) == 0 || s.warnings[len(s.warnings)-1] != warning {
				s.warnings = append(s.warnings, warning)
			}
		}
		return
	}
	if err == nil {
		err = os.MkdirAll(filepath.Dir(s.path), 0o700)
	}
	if err == nil {
		var file *os.File
		file, err = os.CreateTemp(filepath.Dir(s.path), ".messages-*.tmp")
		if err == nil {
			name := file.Name()
			_, err = file.Write(data)
			if err == nil {
				err = file.Sync()
			}
			closeErr := file.Close()
			if err == nil {
				err = closeErr
			}
			if err == nil {
				err = os.Rename(name, s.path)
			}
			if err != nil {
				_ = os.Remove(name)
			}
		}
	}
	if err != nil {
		// Persistence failures are surfaced in the existing management warning list.
		warning := "message store persistence failed: " + err.Error()
		if len(s.warnings) == 0 || s.warnings[len(s.warnings)-1] != warning {
			s.warnings = append(s.warnings, warning)
		}
	}
}

func (s *Store) Create(message Message, targetClients []string, now time.Time) (Record, error) {
	if message.ID.IsZero() {
		return Record{}, errors.New("message ID is required")
	}
	if !message.PrioritySet && message.Priority == 0 {
		message.Priority = DefaultPriority
	}
	message.PrioritySet = true
	if message.CreatedAt == 0 {
		message.CreatedAt = now.UnixMilli()
	}
	if message.ExpiresAt == 0 {
		message.ExpiresAt = now.Add(24 * time.Hour).UnixMilli()
	}
	if message.ExpiresAt <= message.CreatedAt {
		return Record{}, errors.New("message expiry must be after creation")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.pruneLocked(now)
	if existing, ok := s.records[message.ID]; ok {
		return existing, nil
	}
	queuedMessage, err := s.queue.EnqueueWithSequence(message)
	if err != nil {
		return Record{}, err
	}
	message = queuedMessage
	deliveries := make(map[string]DeliveryStatus, len(targetClients))
	for _, clientID := range targetClients {
		if clientID != "" {
			deliveries[clientID] = DeliveryPending
		}
	}
	record := Record{Message: message, TargetClients: append([]string(nil), targetClients...), Status: DeliveryPending, CreatedAt: time.UnixMilli(message.CreatedAt), ExpiresAt: time.UnixMilli(message.ExpiresAt), Deliveries: deliveries}
	s.records[message.ID] = record
	s.persistLocked()
	return record, nil
}

func (s *Store) List(now time.Time) []Record {
	s.Expire(now)
	s.mu.RLock()
	defer s.mu.RUnlock()
	items := make([]Record, 0, len(s.records))
	for _, record := range s.records {
		record.TargetClients = append([]string(nil), record.TargetClients...)
		record.Deliveries = cloneDeliveries(record.Deliveries)
		items = append(items, record)
	}
	sort.Slice(items, func(i, j int) bool {
		return items[i].Message.QueueSeq < items[j].Message.QueueSeq
	})
	return items
}

func (s *Store) Expire(now time.Time) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	count := s.pruneLocked(now)
	for id, record := range s.records {
		if record.Status == DeliveryPending && !now.Before(record.ExpiresAt) {
			record.Status = DeliveryExpired
			s.records[id] = record
			s.warnings = append(s.warnings, "message "+id.String()+" expired before delivery")
			count++
		}
	}
	if count > 0 {
		s.persistLocked()
	}
	return count
}

func (s *Store) Warnings() []string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return append([]string(nil), s.warnings...)
}

func (s *Store) NextPending(now time.Time) *Record {
	s.Expire(now)
	message := s.queue.Pop()
	if message == nil {
		return nil
	}
	s.mu.RLock()
	record, ok := s.records[message.ID]
	s.mu.RUnlock()
	if !ok || record.Status != DeliveryPending {
		return nil
	}
	return &record
}

func (s *Store) Retry(messageID MessageID) bool {
	s.mu.RLock()
	record, ok := s.records[messageID]
	s.mu.RUnlock()
	if !ok || record.Status != DeliveryPending {
		return false
	}
	return s.queue.Requeue(record.Message) == nil
}

// Complete marks a message as delivered to all recipients selected by the
// dispatcher. It is intentionally idempotent so reconnect/retry paths can
// safely acknowledge the same message more than once.
func (s *Store) Complete(messageID MessageID, now time.Time) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	record, ok := s.records[messageID]
	if !ok || record.Status != DeliveryPending || !now.Before(record.ExpiresAt) {
		return false
	}
	record.Status = DeliverySent
	s.records[messageID] = record
	s.persistLocked()
	return true
}

func (s *Store) MarkSent(messageID MessageID, clientID string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	record, ok := s.records[messageID]
	if !ok || record.Status == DeliveryExpired || clientID == "" {
		return false
	}
	if len(record.TargetClients) > 0 && !containsClient(record.TargetClients, clientID) {
		return false
	}
	if record.Deliveries == nil {
		record.Deliveries = make(map[string]DeliveryStatus)
	}
	if current, exists := record.Deliveries[clientID]; exists && current != DeliveryPending {
		return true
	}
	record.Deliveries[clientID] = DeliverySent
	s.records[messageID] = record
	s.persistLocked()
	return true
}

func containsClient(clients []string, clientID string) bool {
	for _, candidate := range clients {
		if candidate == clientID {
			return true
		}
	}
	return false
}

func (s *Store) Acknowledge(messageID MessageID, clientID string, status DeliveryStatus) bool {
	if status != DeliveryReceived && status != DeliveryDisplayed && status != DeliverySpoken && status != DeliveryFailed && status != DeliveryWithdrawn {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	record, ok := s.records[messageID]
	if !ok || record.Status == DeliveryExpired || clientID == "" {
		return false
	}
	if record.Status == DeliveryWithdrawn {
		if status != DeliveryWithdrawn {
			return false
		}
		if _, wasSent := record.Deliveries[clientID]; !wasSent {
			return false
		}
		record.Deliveries[clientID] = DeliveryWithdrawn
		s.records[messageID] = record
		s.persistLocked()
		return true
	}
	if status == DeliveryWithdrawn {
		return false
	}
	if len(record.TargetClients) > 0 && !containsClient(record.TargetClients, clientID) {
		return false
	}
	if record.Deliveries == nil {
		record.Deliveries = make(map[string]DeliveryStatus)
	}
	record.Deliveries[clientID] = status
	s.records[messageID] = record
	s.persistLocked()
	return true
}

// Withdraw makes a message unavailable for future delivery. Existing clients
// may still receive a withdrawal event so their UI can explain the change.
func (s *Store) Withdraw(messageID MessageID, _ time.Time) (Record, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	record, ok := s.records[messageID]
	if !ok || record.Status == DeliveryExpired || record.Status == DeliveryWithdrawn {
		return Record{}, false
	}
	record.Status = DeliveryWithdrawn
	s.records[messageID] = record
	s.warnings = append(s.warnings, "message "+messageID.String()+" withdrawn")
	s.persistLocked()
	return record, true
}

func cloneDeliveries(input map[string]DeliveryStatus) map[string]DeliveryStatus {
	if len(input) == 0 {
		return nil
	}
	output := make(map[string]DeliveryStatus, len(input))
	for clientID, status := range input {
		output[clientID] = status
	}
	return output
}
