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

// HistoryRetention is how long finished/expired/withdrawn message records are kept.
// Active (pending/sent) messages are not pruned by age alone.
const HistoryRetention = 185 * 24 * time.Hour

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
	// DeliveryHistory 在撤回后保留完成回执，供界面显示累计接收/显示数量，
	// 但不会因此恢复消息投递。
	DeliveryHistory   map[string]DeliveryStatus `json:"delivery_history,omitempty"`
	DeliveryUpdatedAt map[string]int64          `json:"-"`
}

type Store struct {
	mu       sync.RWMutex
	records  map[MessageID]Record
	queue    *Queue
	warnings []string
	path     string
	db       *sql.DB
	// lastCreatedAt 是服务端持久化的单调时间排序键，不是消息唯一标识。
	// 它避免同一毫秒创建的消息和服务端重启后重新从 1 开始的 seq 破坏 FIFO。
	lastCreatedAt int64
	queued        map[MessageID]bool
	retryAt       map[MessageID]int64
}

func NewStore() *Store {
	return &Store{records: make(map[MessageID]Record), queue: NewQueue(), queued: make(map[MessageID]bool), retryAt: make(map[MessageID]int64)}
}

// NewPersistentStore restores normalized SQLite message records (or the test
// JSON file backend) and requeues messages that were still pending.
func NewPersistentStore(path string) (*Store, error) {
	s := NewStore()
	s.path = path
	var data []byte
	var err error
	if filepath.Ext(path) == ".db" {
		if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
			return nil, fmt.Errorf("create message store directory: %w", err)
		}
		s.db, err = sql.Open("sqlite", path)
		if err == nil {
			err = s.db.Ping()
		}
		if err == nil {
			err = s.loadSQLite()
		}
	} else {
		data, err = os.ReadFile(path)
	}
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("read message store: %w", err)
	}
	// SQLite 使用规范化记录表加载完成；只有测试和旧的文件存储路径才解析单个 JSON 快照。
	if s.db != nil {
		if err := s.restorePending(); err != nil {
			_ = s.db.Close()
			return nil, err
		}
		return s, nil
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
		if record.Message.CreatedAt > s.lastCreatedAt {
			s.lastCreatedAt = record.Message.CreatedAt
		}
		if record.Status == DeliveryPending {
			if _, err := s.queue.EnqueueWithSequence(record.Message); err != nil {
				return nil, fmt.Errorf("restore pending message: %w", err)
			}
		}
	}
	return s, nil
}

// loadSQLite 从规范化的消息、目标和回执表恢复内存索引。
// 消息正文仍是一行一条记录，不能把整个消息集合塞进单个 JSON BLOB。
func (s *Store) loadSQLite() error {
	_, err := s.db.Exec(`
CREATE TABLE IF NOT EXISTS messages (
	message_id TEXT PRIMARY KEY,
	queue_seq INTEGER NOT NULL DEFAULT 0,
	priority INTEGER NOT NULL,
	priority_set INTEGER NOT NULL DEFAULT 0,
	content TEXT NOT NULL,
	display_position TEXT,
	display_duration_ratio REAL,
	as_default INTEGER NOT NULL DEFAULT 0,
	speech_json BLOB NOT NULL DEFAULT 'null',
	created_at INTEGER NOT NULL,
	expires_at INTEGER NOT NULL,
	status TEXT NOT NULL,
	updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_created_at_idx ON messages(created_at DESC, message_id DESC);
CREATE TABLE IF NOT EXISTS message_targets (
	message_id TEXT NOT NULL,
	client_id TEXT NOT NULL,
	PRIMARY KEY(message_id, client_id)
);
CREATE INDEX IF NOT EXISTS message_targets_message_idx ON message_targets(message_id);
CREATE TABLE IF NOT EXISTS message_deliveries (
	message_id TEXT NOT NULL,
	client_id TEXT NOT NULL,
	status TEXT NOT NULL,
	last_attempt_at INTEGER NOT NULL DEFAULT 0,
	PRIMARY KEY(message_id, client_id)
);
CREATE INDEX IF NOT EXISTS message_deliveries_message_idx ON message_deliveries(message_id);
CREATE TABLE IF NOT EXISTS message_delivery_history (
	message_id TEXT NOT NULL,
	client_id TEXT NOT NULL,
	status TEXT NOT NULL,
	PRIMARY KEY(message_id, client_id)
);
CREATE TABLE IF NOT EXISTS store_warnings (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	warning TEXT NOT NULL,
	created_at INTEGER NOT NULL
);`)
	if err != nil {
		return fmt.Errorf("create message tables: %w", err)
	}
	if err := ensureColumn(s.db, "message_deliveries", "last_attempt_at", "INTEGER NOT NULL DEFAULT 0"); err != nil {
		return fmt.Errorf("upgrade message deliveries: %w", err)
	}
	if err := ensureColumn(s.db, "messages", "as_default", "INTEGER NOT NULL DEFAULT 0"); err != nil {
		return fmt.Errorf("upgrade messages: %w", err)
	}
	rows, err := s.db.Query(`SELECT message_id, queue_seq, priority, priority_set, content, display_position, display_duration_ratio, as_default, speech_json, created_at, expires_at, status FROM messages ORDER BY created_at ASC, message_id ASC`)
	if err != nil {
		return fmt.Errorf("load messages: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var idText, content, status string
		var queueSeq, priority, prioritySet, asDefault, createdAt, expiresAt int64
		var position, speechJSON sql.NullString
		var ratio sql.NullFloat64
		if err := rows.Scan(&idText, &queueSeq, &priority, &prioritySet, &content, &position, &ratio, &asDefault, &speechJSON, &createdAt, &expiresAt, &status); err != nil {
			return fmt.Errorf("scan message: %w", err)
		}
		id, err := ParseMessageID(idText)
		if err != nil {
			return fmt.Errorf("invalid message id %q: %w", idText, err)
		}
		message := Message{ID: id, QueueSeq: uint64(maxInt64(queueSeq, 0)), Priority: uint16(maxInt64(priority, 0)), PrioritySet: prioritySet != 0, DisplayText: content, AsDefault: asDefault != 0, CreatedAt: createdAt, ExpiresAt: expiresAt}
		if position.Valid {
			value := position.String
			message.DisplayPosition = &value
		}
		if ratio.Valid {
			value := ratio.Float64
			message.DisplayDurationRatio = &value
		}
		if speechJSON.Valid && speechJSON.String != "" && speechJSON.String != "null" {
			if err := json.Unmarshal([]byte(speechJSON.String), &message.Speech); err != nil {
				return fmt.Errorf("decode speech for %s: %w", idText, err)
			}
		}
		s.records[id] = Record{Message: message, Status: DeliveryStatus(status), CreatedAt: time.UnixMilli(createdAt), ExpiresAt: time.UnixMilli(expiresAt)}
		if createdAt > s.lastCreatedAt {
			s.lastCreatedAt = createdAt
		}
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("iterate messages: %w", err)
	}
	// 先恢复目标，再恢复回执；目标初始化不能覆盖数据库中已经保存的最终状态。
	for _, target := range []bool{true, false} {
		table := "message_deliveries"
		selectColumns := ", status, last_attempt_at"
		if target {
			table = "message_targets"
			selectColumns = ""
		}
		rows, err := s.db.Query(`SELECT message_id, client_id` + selectColumns + ` FROM ` + table + ` ORDER BY message_id, client_id`)
		if err != nil {
			return fmt.Errorf("load %s: %w", table, err)
		}
		for rows.Next() {
			var idText, clientID string
			if target {
				if err := rows.Scan(&idText, &clientID); err != nil {
					rows.Close()
					return fmt.Errorf("scan message target: %w", err)
				}
				id, err := ParseMessageID(idText)
				if err != nil {
					rows.Close()
					return err
				}
				record, ok := s.records[id]
				if ok {
					record.TargetClients = append(record.TargetClients, clientID)
					if record.Deliveries == nil {
						record.Deliveries = make(map[string]DeliveryStatus)
					}
					if _, exists := record.Deliveries[clientID]; !exists {
						record.Deliveries[clientID] = DeliveryPending
					}
					s.records[id] = record
				}
			} else {
				var status string
				var lastAttemptAt int64
				if err := rows.Scan(&idText, &clientID, &status, &lastAttemptAt); err != nil {
					rows.Close()
					return fmt.Errorf("scan message delivery: %w", err)
				}
				id, err := ParseMessageID(idText)
				if err != nil {
					rows.Close()
					return err
				}
				record, ok := s.records[id]
				if ok {
					if record.Deliveries == nil {
						record.Deliveries = make(map[string]DeliveryStatus)
					}
					if record.DeliveryUpdatedAt == nil {
						record.DeliveryUpdatedAt = make(map[string]int64)
					}
					record.Deliveries[clientID] = DeliveryStatus(status)
					record.DeliveryUpdatedAt[clientID] = lastAttemptAt
					s.records[id] = record
				}
			}
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return fmt.Errorf("iterate %s: %w", table, err)
		}
		rows.Close()
	}
	rows, err = s.db.Query(`SELECT message_id, client_id, status FROM message_delivery_history ORDER BY message_id, client_id`)
	if err != nil {
		return fmt.Errorf("load message delivery history: %w", err)
	}
	for rows.Next() {
		var idText, clientID, status string
		if err := rows.Scan(&idText, &clientID, &status); err != nil {
			rows.Close()
			return fmt.Errorf("scan message delivery history: %w", err)
		}
		id, err := ParseMessageID(idText)
		if err != nil {
			rows.Close()
			return err
		}
		if record, ok := s.records[id]; ok {
			if record.DeliveryHistory == nil {
				record.DeliveryHistory = make(map[string]DeliveryStatus)
			}
			record.DeliveryHistory[clientID] = DeliveryStatus(status)
			s.records[id] = record
		}
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return fmt.Errorf("iterate message delivery history: %w", err)
	}
	rows.Close()
	warningRows, err := s.db.Query(`SELECT warning FROM store_warnings ORDER BY id ASC`)
	if err != nil {
		return fmt.Errorf("load warnings: %w", err)
	}
	defer warningRows.Close()
	for warningRows.Next() {
		var warning string
		if err := warningRows.Scan(&warning); err != nil {
			return err
		}
		s.warnings = append(s.warnings, warning)
	}
	return warningRows.Err()
}

func maxInt64(value, fallback int64) int64 {
	if value < fallback {
		return fallback
	}
	return value
}

// ensureColumn 为开发期数据库做幂等列升级，不从旧 JSON 导入数据。
func ensureColumn(db *sql.DB, table, column, definition string) error {
	rows, err := db.Query(`PRAGMA table_info(` + table + `)`)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var cid int
		var name, dataType string
		var notNull, primaryKey int
		var defaultValue any
		if err := rows.Scan(&cid, &name, &dataType, &notNull, &defaultValue, &primaryKey); err != nil {
			return err
		}
		if name == column {
			return rows.Err()
		}
	}
	if err := rows.Err(); err != nil {
		return err
	}
	_, err = db.Exec(`ALTER TABLE ` + table + ` ADD COLUMN ` + column + ` ` + definition)
	return err
}

// restorePending 将数据库中仍处于 pending 的消息放回内存优先级队列。
func (s *Store) restorePending() error {
	for _, record := range s.records {
		if record.Status == DeliveryPending {
			if _, err := s.queue.EnqueueWithSequence(record.Message); err != nil {
				return fmt.Errorf("restore pending message: %w", err)
			}
			s.queued[record.Message.ID] = true
		}
	}
	return nil
}

func (s *Store) pruneLocked(now time.Time) int {
	cutoff := now.Add(-HistoryRetention)
	removed := 0
	for id, record := range s.records {
		// Keep live deliveries even if they are older than retention so a
		// long-running classroom message is not dropped mid-flight.
		if record.Status == DeliveryPending || record.Status == DeliverySent {
			continue
		}
		if !record.CreatedAt.After(cutoff) {
			delete(s.records, id)
			delete(s.queued, id)
			delete(s.retryAt, id)
			removed++
		}
	}
	return removed
}

// PurgeOlderThan removes every message record whose created_at is at least
// maxAge old (including pending/sent). Used by the daily retention job.
func (s *Store) PurgeOlderThan(now time.Time, maxAge time.Duration) int {
	if maxAge <= 0 {
		maxAge = HistoryRetention
	}
	cutoff := now.Add(-maxAge)
	s.mu.Lock()
	defer s.mu.Unlock()
	removed := 0
	for id, record := range s.records {
		if !record.CreatedAt.After(cutoff) {
			delete(s.records, id)
			delete(s.queued, id)
			delete(s.retryAt, id)
			removed++
		}
	}
	if removed > 0 {
		s.persistLocked()
	}
	return removed
}

func (s *Store) persistLocked() {
	if s.path == "" && s.db == nil {
		return
	}
	if s.db != nil {
		if err := s.persistSQLiteLocked(); err != nil {
			warning := "message store persistence failed: " + err.Error()
			if len(s.warnings) == 0 || s.warnings[len(s.warnings)-1] != warning {
				s.warnings = append(s.warnings, warning)
			}
		}
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
		return messageBefore(snapshot.Records[i].Message, snapshot.Records[j].Message)
	})
	data, err := json.Marshal(snapshot)
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

// persistSQLiteLocked 以事务方式更新规范化记录表，保证消息正文、目标和回执的一致性。
func (s *Store) persistSQLiteLocked() error {
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	rollback := func(e error) error { _ = tx.Rollback(); return e }
	if _, err = tx.Exec(`DELETE FROM message_deliveries`); err != nil {
		return rollback(err)
	}
	if _, err = tx.Exec(`DELETE FROM message_delivery_history`); err != nil {
		return rollback(err)
	}
	if _, err = tx.Exec(`DELETE FROM message_targets`); err != nil {
		return rollback(err)
	}
	if _, err = tx.Exec(`DELETE FROM messages`); err != nil {
		return rollback(err)
	}
	if _, err = tx.Exec(`DELETE FROM store_warnings`); err != nil {
		return rollback(err)
	}
	messageStmt, err := tx.Prepare(`INSERT INTO messages(message_id, queue_seq, priority, priority_set, content, display_position, display_duration_ratio, as_default, speech_json, created_at, expires_at, status, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
	if err != nil {
		return rollback(err)
	}
	targetStmt, err := tx.Prepare(`INSERT INTO message_targets(message_id, client_id) VALUES (?, ?)`)
	if err != nil {
		messageStmt.Close()
		return rollback(err)
	}
	deliveryStmt, err := tx.Prepare(`INSERT INTO message_deliveries(message_id, client_id, status, last_attempt_at) VALUES (?, ?, ?, ?)`)
	if err != nil {
		messageStmt.Close()
		targetStmt.Close()
		return rollback(err)
	}
	historyStmt, err := tx.Prepare(`INSERT INTO message_delivery_history(message_id, client_id, status) VALUES (?, ?, ?)`)
	if err != nil {
		messageStmt.Close()
		targetStmt.Close()
		deliveryStmt.Close()
		return rollback(err)
	}
	warningStmt, err := tx.Prepare(`INSERT INTO store_warnings(warning, created_at) VALUES (?, ?)`)
	if err != nil {
		messageStmt.Close()
		targetStmt.Close()
		deliveryStmt.Close()
		return rollback(err)
	}
	defer messageStmt.Close()
	defer targetStmt.Close()
	defer deliveryStmt.Close()
	defer historyStmt.Close()
	defer warningStmt.Close()
	ordered := make([]Record, 0, len(s.records))
	for _, record := range s.records {
		ordered = append(ordered, record)
	}
	sort.Slice(ordered, func(i, j int) bool { return messageBefore(ordered[i].Message, ordered[j].Message) })
	for _, record := range ordered {
		speech, marshalErr := json.Marshal(record.Message.Speech)
		if marshalErr != nil {
			return rollback(marshalErr)
		}
		var position any
		if record.Message.DisplayPosition != nil {
			position = *record.Message.DisplayPosition
		}
		var ratio any
		if record.Message.DisplayDurationRatio != nil {
			ratio = *record.Message.DisplayDurationRatio
		}
		if _, err = messageStmt.Exec(record.Message.ID.String(), int64(record.Message.QueueSeq), record.Message.Priority, boolInt(record.Message.PrioritySet), record.Message.DisplayText, position, ratio, boolInt(record.Message.AsDefault), string(speech), record.Message.CreatedAt, record.Message.ExpiresAt, record.Status, time.Now().UnixMilli()); err != nil {
			return rollback(err)
		}
		for _, clientID := range record.TargetClients {
			if clientID != "" {
				if _, err = targetStmt.Exec(record.Message.ID.String(), clientID); err != nil {
					return rollback(err)
				}
			}
		}
		for clientID, status := range record.Deliveries {
			if clientID != "" {
				if _, err = deliveryStmt.Exec(record.Message.ID.String(), clientID, status, record.DeliveryUpdatedAt[clientID]); err != nil {
					return rollback(err)
				}
			}
		}
		for clientID, status := range record.DeliveryHistory {
			if clientID != "" {
				if _, err = historyStmt.Exec(record.Message.ID.String(), clientID, status); err != nil {
					return rollback(err)
				}
			}
		}
	}
	for _, warning := range s.warnings {
		if _, err = warningStmt.Exec(warning, time.Now().UnixMilli()); err != nil {
			return rollback(err)
		}
	}
	return tx.Commit()
}

func boolInt(value bool) int {
	if value {
		return 1
	}
	return 0
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
	// 创建时间同时承担持久化排序键；同一毫秒内的消息递增 1ms，避免依赖会在重启后复用的 seq。
	if message.CreatedAt <= s.lastCreatedAt {
		message.CreatedAt = s.lastCreatedAt + 1
		if message.ExpiresAt <= message.CreatedAt {
			message.ExpiresAt = message.CreatedAt + 24*time.Hour.Milliseconds()
		}
	}
	s.lastCreatedAt = message.CreatedAt
	queuedMessage, err := s.queue.EnqueueWithSequence(message)
	if err != nil {
		return Record{}, err
	}
	message = queuedMessage
	deliveries := make(map[string]DeliveryStatus, len(targetClients))
	deliveryUpdatedAt := make(map[string]int64, len(targetClients))
	for _, clientID := range targetClients {
		// "*" is a live broadcast marker, not a real delivery target. Storing it
		// as pending would make QueueDueRetries requeue the message forever.
		if clientID != "" && clientID != "*" {
			deliveries[clientID] = DeliveryPending
			deliveryUpdatedAt[clientID] = 0
		}
	}
	record := Record{Message: message, TargetClients: append([]string(nil), targetClients...), Status: DeliveryPending, CreatedAt: time.UnixMilli(message.CreatedAt), ExpiresAt: time.UnixMilli(message.ExpiresAt), Deliveries: deliveries, DeliveryUpdatedAt: deliveryUpdatedAt}
	s.records[message.ID] = record
	s.queued[message.ID] = true
	s.persistLocked()
	return record, nil
}

// AddClientToBroadcasts adds a newly approved client to live wildcard broadcasts.
func (s *Store) AddClientToBroadcasts(clientID string, now time.Time) int {
	if clientID == "" {
		return 0
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	count := 0
	for id, record := range s.records {
		if !containsClient(record.TargetClients, "*") || !now.Before(record.ExpiresAt) || record.Status == DeliveryExpired || record.Status == DeliveryWithdrawn {
			continue
		}
		if record.Deliveries == nil {
			record.Deliveries = make(map[string]DeliveryStatus)
		}
		if _, exists := record.Deliveries[clientID]; exists {
			continue
		}
		record.Deliveries[clientID] = DeliveryPending
		if record.DeliveryUpdatedAt == nil {
			record.DeliveryUpdatedAt = make(map[string]int64)
		}
		s.records[id] = record
		if !s.queued[id] {
			if err := s.queue.Requeue(record.Message); err == nil {
				s.queued[id] = true
			}
		}
		count++
	}
	if count > 0 {
		s.persistLocked()
	}
	return count
}

func (s *Store) List(now time.Time) []Record {
	s.Expire(now)
	s.mu.RLock()
	defer s.mu.RUnlock()
	items := make([]Record, 0, len(s.records))
	for _, record := range s.records {
		record.TargetClients = append([]string(nil), record.TargetClients...)
		record.Deliveries = cloneDeliveries(record.Deliveries)
		record.DeliveryHistory = cloneDeliveries(record.DeliveryHistory)
		record.DeliveryUpdatedAt = cloneUpdatedAt(record.DeliveryUpdatedAt)
		items = append(items, record)
	}
	sort.Slice(items, func(i, j int) bool { return messageAfter(items[i].Message, items[j].Message) })
	return items
}

func (s *Store) Expire(now time.Time) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	count := s.pruneLocked(now)
	for id, record := range s.records {
		if (record.Status == DeliveryPending || record.Status == DeliverySent) && !now.Before(record.ExpiresAt) {
			record.Status = DeliveryExpired
			for clientID, status := range record.Deliveries {
				// 过期只结束尚未收到的投递；received/displayed/spoken 是历史回执，
				// 不能被过期事件覆盖后让前端计数回到 0。
				if status == DeliveryPending || status == DeliverySent {
					record.Deliveries[clientID] = DeliveryExpired
				}
			}
			s.records[id] = record
			s.warnings = append(s.warnings, "message "+id.String()+" expired before delivery acknowledgement")
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
	s.mu.Lock()
	delete(s.queued, message.ID)
	record, ok := s.records[message.ID]
	if !ok || (record.Status != DeliveryPending && record.Status != DeliverySent) {
		s.mu.Unlock()
		return nil
	}
	if dueAt := s.retryAt[message.ID]; dueAt > now.UnixMilli() {
		s.mu.Unlock()
		return nil
	}
	delete(s.retryAt, message.ID)
	record.TargetClients = append([]string(nil), record.TargetClients...)
	record.Deliveries = cloneDeliveries(record.Deliveries)
	record.DeliveryUpdatedAt = cloneUpdatedAt(record.DeliveryUpdatedAt)
	s.mu.Unlock()
	return &record
}

func (s *Store) Retry(messageID MessageID) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	record, ok := s.records[messageID]
	if !ok || (record.Status != DeliveryPending && record.Status != DeliverySent) || s.queued[messageID] {
		return false
	}
	if err := s.queue.Requeue(record.Message); err != nil {
		return false
	}
	s.queued[messageID] = true
	delete(s.retryAt, messageID)
	return true
}

// RetryAfter 在下一次重试时间前保持记录持久化但不忙轮询队列。
func (s *Store) RetryAfter(messageID MessageID, at time.Time) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	record, ok := s.records[messageID]
	if !ok || (record.Status != DeliveryPending && record.Status != DeliverySent) {
		return false
	}
	s.retryAt[messageID] = at.UnixMilli()
	return true
}

// QueueDueRetries 将离线待投递记录和超时未完成回执的消息重新排队。
func (s *Store) QueueDueRetries(now time.Time, ackTimeout time.Duration) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	queued := 0
	nowMS := now.UnixMilli()
	for id, record := range s.records {
		if s.queued[id] || s.retryAt[id] > nowMS || !now.Before(record.ExpiresAt) || (record.Status != DeliveryPending && record.Status != DeliverySent) {
			continue
		}
		due := false
		hasRealDelivery := false
		for clientID, status := range record.Deliveries {
			if clientID == "*" {
				continue
			}
			hasRealDelivery = true
			if status == DeliveryPending {
				due = true
				break
			}
			if status == DeliverySent || status == DeliveryReceived {
				lastAttempt := record.DeliveryUpdatedAt[clientID]
				if lastAttempt == 0 || nowMS-lastAttempt >= ackTimeout.Milliseconds() {
					due = true
					break
				}
			}
		}
		// Wildcard-only broadcasts wait for AddClientToBroadcasts; do not spin.
		if !due && !hasRealDelivery && containsClient(record.TargetClients, "*") {
			continue
		}
		if !due {
			continue
		}
		if err := s.queue.Requeue(record.Message); err == nil {
			s.queued[id] = true
			delete(s.retryAt, id)
			queued++
		}
	}
	return queued
}

// ShouldRedeliver 判断单个接收者是否仍待投递，或已超过应用层回执超时。
func (s *Store) ShouldRedeliver(messageID MessageID, clientID string, now time.Time, ackTimeout time.Duration) bool {
	s.mu.RLock()
	defer s.mu.RUnlock()
	record, ok := s.records[messageID]
	if !ok || !now.Before(record.ExpiresAt) {
		return false
	}
	status, ok := record.Deliveries[clientID]
	if !ok {
		return false
	}
	if status == DeliveryPending {
		return true
	}
	if status != DeliverySent && status != DeliveryReceived {
		return false
	}
	lastAttempt := record.DeliveryUpdatedAt[clientID]
	return lastAttempt == 0 || now.UnixMilli()-lastAttempt >= ackTimeout.Milliseconds()
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

func (s *Store) MarkSent(messageID MessageID, clientID string, sentAt ...time.Time) bool {
	when := time.Now()
	checkExpiry := false
	if len(sentAt) > 0 {
		when = sentAt[0]
		checkExpiry = true
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	record, ok := s.records[messageID]
	if !ok || record.Status == DeliveryExpired || clientID == "" || (checkExpiry && !when.Before(record.ExpiresAt)) {
		return false
	}
	if len(record.TargetClients) > 0 && !containsClient(record.TargetClients, clientID) && !containsClient(record.TargetClients, "*") {
		return false
	}
	if record.Deliveries == nil {
		record.Deliveries = make(map[string]DeliveryStatus)
	}
	current := record.Deliveries[clientID]
	switch current {
	case DeliveryDisplayed, DeliverySpoken, DeliveryFailed, DeliveryWithdrawn:
		return true
	case DeliveryPending:
		record.Deliveries[clientID] = DeliverySent
	}
	if record.DeliveryUpdatedAt == nil {
		record.DeliveryUpdatedAt = make(map[string]int64)
	}
	record.DeliveryUpdatedAt[clientID] = when.UnixMilli()
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
		// 撤回回执不能抹掉客户端已经显示或播报的历史结果；前端仍应
		// 能看到“已显示完成/已接收”的累计数量。
		current := record.Deliveries[clientID]
		if current == DeliveryReceived || current == DeliveryDisplayed || current == DeliverySpoken || current == DeliveryFailed {
			if record.DeliveryHistory == nil {
				record.DeliveryHistory = make(map[string]DeliveryStatus)
			}
			if deliveryStatusRank(current) >= deliveryStatusRank(record.DeliveryHistory[clientID]) {
				record.DeliveryHistory[clientID] = current
			}
		}
		record.Deliveries[clientID] = DeliveryWithdrawn
		s.records[messageID] = record
		s.persistLocked()
		return true
	}
	if status == DeliveryWithdrawn {
		return false
	}
	if len(record.TargetClients) > 0 && !containsClient(record.TargetClients, clientID) && !containsClient(record.TargetClients, "*") {
		return false
	}
	if record.Deliveries == nil {
		record.Deliveries = make(map[string]DeliveryStatus)
	}
	current := record.Deliveries[clientID]
	if (current == DeliveryDisplayed || current == DeliverySpoken) &&
		(status == DeliveryReceived || status == DeliveryFailed || (current == DeliverySpoken && status == DeliveryDisplayed)) {
		return true
	}
	if status == DeliveryReceived || status == DeliveryDisplayed || status == DeliverySpoken || status == DeliveryFailed {
		if record.DeliveryHistory == nil {
			record.DeliveryHistory = make(map[string]DeliveryStatus)
		}
		// Keep the most advanced receipt so UI "已显示完成" never regresses to received-only.
		if deliveryStatusRank(status) >= deliveryStatusRank(record.DeliveryHistory[clientID]) {
			record.DeliveryHistory[clientID] = status
		}
	}
	record.Deliveries[clientID] = status
	// 回执不能刷新重传计时：重复的 received 回执可能因重传反复到达，
	// 只有服务端实际写出消息时才更新 last_attempt_at。
	s.records[messageID] = record
	s.persistLocked()
	return true
}

// deliveryStatusRank orders client receipts for monotonic DeliveryHistory updates.
func deliveryStatusRank(status DeliveryStatus) int {
	switch status {
	case DeliverySpoken:
		return 5
	case DeliveryDisplayed:
		return 4
	case DeliveryFailed:
		return 3
	case DeliveryReceived:
		return 2
	case DeliverySent:
		return 1
	case DeliveryPending, DeliveryExpired, DeliveryWithdrawn:
		return 0
	default:
		return -1
	}
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

func cloneUpdatedAt(input map[string]int64) map[string]int64 {
	if len(input) == 0 {
		return nil
	}
	output := make(map[string]int64, len(input))
	for clientID, value := range input {
		output[clientID] = value
	}
	return output
}
