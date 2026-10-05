package broadcast

import (
	"database/sql"
	"path/filepath"
	"testing"
	"time"

	_ "modernc.org/sqlite"
)

func TestStoreDefaultsAndIdempotency(t *testing.T) {
	id, err := NewMessageID()
	if err != nil {
		t.Fatal(err)
	}
	if id.String()[14] != '4' {
		t.Fatalf("message id is not UUID4: %s", id)
	}
	now := time.Unix(1_700_000_000, 0)
	store := NewStore()
	record, err := store.Create(Message{ID: id, DisplayText: "hello"}, []string{"client"}, now)
	if err != nil {
		t.Fatal(err)
	}
	if record.Message.Priority != DefaultPriority || record.ExpiresAt.Sub(now) != 24*time.Hour {
		t.Fatalf("unexpected defaults: %#v", record)
	}
	urgentID, err := NewMessageID()
	if err != nil {
		t.Fatal(err)
	}
	urgent, err := store.Create(Message{ID: urgentID, PrioritySet: true, Priority: 0, DisplayText: "urgent"}, nil, now)
	if err != nil || urgent.Message.Priority != 0 {
		t.Fatalf("explicit priority zero was not preserved: %#v %v", urgent, err)
	}
	duplicate, err := store.Create(Message{ID: id, Priority: 1, DisplayText: "other"}, nil, now)
	if err != nil || duplicate.Message.DisplayText != "hello" {
		t.Fatalf("idempotency failed: %#v %v", duplicate, err)
	}
}

func TestPersistentStoreRestoresPendingAndHistory(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	path := filepath.Join(t.TempDir(), "data", "messages.json")
	store, err := NewPersistentStore(path)
	if err != nil {
		t.Fatal(err)
	}
	pendingID, _ := NewMessageID()
	completedID, _ := NewMessageID()
	if _, err := store.Create(Message{ID: pendingID, DisplayText: "pending"}, nil, now); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Create(Message{ID: completedID, DisplayText: "sent"}, nil, now); err != nil {
		t.Fatal(err)
	}
	if next := store.NextPending(now); next == nil || next.Message.ID != pendingID {
		t.Fatal("pending message was not queued")
	}
	if !store.Complete(pendingID, now) {
		t.Fatal("could not persist completed state")
	}
	restored, err := NewPersistentStore(path)
	if err != nil {
		t.Fatal(err)
	}
	items := restored.List(now)
	if len(items) != 2 || items[0].Message.QueueSeq == 0 || items[0].Status != DeliveryPending || items[1].Status != DeliverySent {
		t.Fatalf("message history was not restored: %#v", items)
	}
	if next := restored.NextPending(now); next == nil || next.Message.ID != completedID {
		t.Fatalf("pending message was not requeued after restart: %#v", next)
	}
}

func TestSQLiteStoreUsesMessageRecordsAndPreservesTimestampOrder(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	path := filepath.Join(t.TempDir(), "data", "server.db")
	store, err := NewPersistentStore(path)
	if err != nil {
		t.Fatal(err)
	}
	first, _ := NewMessageID()
	second, _ := NewMessageID()
	if _, err := store.Create(Message{ID: first, DisplayText: "first"}, nil, now); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Create(Message{ID: second, DisplayText: "second"}, nil, now); err != nil {
		t.Fatal(err)
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var count int
	if err := db.QueryRow(`SELECT COUNT(*) FROM messages`).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 2 {
		t.Fatalf("message rows = %d, want 2", count)
	}
	var snapshotCount int
	if err := db.QueryRow(`SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'store_snapshots'`).Scan(&snapshotCount); err != nil {
		t.Fatal(err)
	}
	if snapshotCount != 0 {
		t.Fatal("message records must not be stored in a store_snapshots JSON blob")
	}
	restored, err := NewPersistentStore(path)
	if err != nil {
		t.Fatal(err)
	}
	items := restored.List(now)
	if len(items) != 2 || items[0].Message.ID != second || items[1].Message.ID != first {
		t.Fatalf("timestamp order was not preserved after restart: %#v", items)
	}
}

func TestStoreExpiresPendingMessages(t *testing.T) {
	id, err := NewMessageID()
	if err != nil {
		t.Fatal(err)
	}
	now := time.Unix(1_700_000_000, 0)
	store := NewStore()
	if _, err := store.Create(Message{ID: id, ExpiresAt: now.Add(time.Second).UnixMilli()}, nil, now); err != nil {
		t.Fatal(err)
	}
	if expired := store.Expire(now.Add(2 * time.Second)); expired != 1 {
		t.Fatalf("expired = %d", expired)
	}
	items := store.List(now.Add(2 * time.Second))
	if len(items) != 1 || items[0].Status != DeliveryExpired || len(store.Warnings()) != 1 {
		t.Fatalf("unexpected expiry state: %#v warnings=%v", items, store.Warnings())
	}
}

func TestStorePrunesHistoryAfterRetention(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	store := NewStore()
	oldID, _ := NewMessageID()
	liveID, _ := NewMessageID()
	newID, _ := NewMessageID()
	if _, err := store.Create(Message{ID: oldID}, nil, now); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Create(Message{ID: liveID}, nil, now); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Create(Message{ID: newID}, nil, now); err != nil {
		t.Fatal(err)
	}
	store.mu.Lock()
	old := store.records[oldID]
	old.CreatedAt = now.Add(-HistoryRetention - time.Hour)
	old.Status = DeliveryDisplayed
	store.records[oldID] = old
	live := store.records[liveID]
	live.CreatedAt = now.Add(-HistoryRetention - time.Hour)
	// pending stays until PurgeOlderThan; Expire/prune skips live deliveries.
	store.records[liveID] = live
	store.mu.Unlock()
	if removed := store.Expire(now); removed != 1 {
		t.Fatalf("Expire removed = %d, want 1 finished record", removed)
	}
	items := store.List(now)
	if len(items) != 2 {
		t.Fatalf("unexpected retained after Expire: %#v", items)
	}
	if purged := store.PurgeOlderThan(now, HistoryRetention); purged != 1 {
		t.Fatalf("PurgeOlderThan removed = %d, want 1 live-but-old record", purged)
	}
	items = store.List(now)
	if len(items) != 1 || items[0].Message.ID != newID {
		t.Fatalf("unexpected retained records: %#v", items)
	}
}

func TestStoreRetryPreservesQueueSequence(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	store := NewStore()
	first, _ := NewMessageID()
	second, _ := NewMessageID()
	if _, err := store.Create(Message{ID: first, PrioritySet: true, Priority: 2}, nil, now); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Create(Message{ID: second, PrioritySet: true, Priority: 1}, nil, now); err != nil {
		t.Fatal(err)
	}
	next := store.NextPending(now)
	if next == nil || next.Message.ID != second {
		t.Fatalf("unexpected next message: %#v", next)
	}
	if !store.Retry(second) {
		t.Fatal("retry rejected")
	}
	retried := store.NextPending(now)
	if retried == nil || retried.Message.ID != second || retried.Message.QueueSeq != next.Message.QueueSeq {
		t.Fatalf("retry changed ordering: %#v", retried)
	}
}

func TestStoreTracksDeliveryAcknowledgements(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	id, _ := NewMessageID()
	store := NewStore()
	if _, err := store.Create(Message{ID: id}, []string{"client"}, now); err != nil {
		t.Fatal(err)
	}
	if !store.MarkSent(id, "client") || !store.Acknowledge(id, "client", DeliveryDisplayed) {
		t.Fatal("delivery acknowledgement rejected")
	}
	items := store.List(now)
	if items[0].Deliveries["client"] != DeliveryDisplayed {
		t.Fatalf("delivery status = %q", items[0].Deliveries["client"])
	}
}

func TestStoreWithdrawsAfterDeliveryAcknowledgement(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	id, _ := NewMessageID()
	store := NewStore()
	if _, err := store.Create(Message{ID: id}, []string{"client"}, now); err != nil {
		t.Fatal(err)
	}
	if !store.MarkSent(id, "client") || !store.Acknowledge(id, "client", DeliveryDisplayed) {
		t.Fatal("delivery acknowledgement rejected")
	}
	record, ok := store.Withdraw(id, now)
	if !ok || record.Status != DeliveryWithdrawn || record.Deliveries["client"] != DeliveryDisplayed {
		t.Fatalf("unexpected withdrawal state: %#v", record)
	}
	if _, ok := store.Withdraw(id, now); ok {
		t.Fatal("second withdrawal should be rejected")
	}
	if !store.Acknowledge(id, "client", DeliveryWithdrawn) {
		t.Fatal("withdrawal receipt rejected")
	}
	if store.Acknowledge(id, "client", DeliveryReceived) {
		t.Fatal("late receipt regressed withdrawn delivery")
	}
	items := store.List(now)
	if items[0].Deliveries["client"] != DeliveryWithdrawn {
		t.Fatalf("withdrawal receipt was not stored: %#v", items[0].Deliveries)
	}
}

func TestStoreRedeliversOnlyAfterAckTimeout(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	id, _ := NewMessageID()
	store := NewStore()
	if _, err := store.Create(Message{ID: id}, []string{"client"}, now); err != nil {
		t.Fatal(err)
	}
	record := store.NextPending(now)
	if record == nil || !store.MarkSent(id, "client", now) {
		t.Fatal("initial delivery was not marked sent")
	}
	store.Complete(id, now)
	if got := store.QueueDueRetries(now.Add(299*time.Second), 300*time.Second); got != 0 {
		t.Fatalf("retry queued before timeout: %d", got)
	}
	if got := store.QueueDueRetries(now.Add(300*time.Second), 300*time.Second); got != 1 {
		t.Fatalf("retry queued after timeout = %d", got)
	}
	retry := store.NextPending(now.Add(300 * time.Second))
	if retry == nil || retry.Message.ID != id {
		t.Fatal("timed out delivery was not requeued")
	}
	if !store.MarkSent(id, "client", now.Add(300*time.Second)) {
		t.Fatal("retry attempt was not recorded")
	}
	if got := store.QueueDueRetries(now.Add(300*time.Second), 300*time.Second); got != 0 {
		t.Fatalf("same retry was queued more than once: %d", got)
	}
}

func TestStoreCompletedReceiptDoesNotRegressOrExtendRetry(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	id, _ := NewMessageID()
	store := NewStore()
	if _, err := store.Create(Message{ID: id}, []string{"client"}, now); err != nil {
		t.Fatal(err)
	}
	if store.NextPending(now) == nil || !store.MarkSent(id, "client", now) {
		t.Fatal("initial delivery was not marked sent")
	}
	store.Complete(id, now)
	if !store.Acknowledge(id, "client", DeliveryDisplayed) {
		t.Fatal("display receipt rejected")
	}
	if !store.Acknowledge(id, "client", DeliveryReceived) {
		t.Fatal("duplicate received receipt should be idempotent")
	}
	item := store.List(now)[0]
	if item.Deliveries["client"] != DeliveryDisplayed {
		t.Fatalf("completed receipt regressed to %q", item.Deliveries["client"])
	}
	if item.DeliveryHistory["client"] != DeliveryDisplayed {
		t.Fatalf("delivery history should keep displayed, got %q", item.DeliveryHistory["client"])
	}
	if got := store.QueueDueRetries(now.Add(24*time.Hour), time.Second); got != 0 {
		t.Fatalf("completed delivery was requeued: %d", got)
	}
}

func TestStoreDeliveryHistoryAdvancesMonotonically(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	id, _ := NewMessageID()
	store := NewStore()
	if _, err := store.Create(Message{ID: id}, []string{"client"}, now); err != nil {
		t.Fatal(err)
	}
	if !store.MarkSent(id, "client", now) {
		t.Fatal("mark sent failed")
	}
	if !store.Acknowledge(id, "client", DeliveryReceived) {
		t.Fatal("received rejected")
	}
	if store.List(now)[0].DeliveryHistory["client"] != DeliveryReceived {
		t.Fatal("history should record received")
	}
	if !store.Acknowledge(id, "client", DeliveryDisplayed) {
		t.Fatal("displayed rejected")
	}
	item := store.List(now)[0]
	if item.Deliveries["client"] != DeliveryDisplayed || item.DeliveryHistory["client"] != DeliveryDisplayed {
		t.Fatalf("history did not advance to displayed: deliveries=%q history=%q", item.Deliveries["client"], item.DeliveryHistory["client"])
	}
	// Late received must not overwrite history or live status.
	if !store.Acknowledge(id, "client", DeliveryReceived) {
		t.Fatal("late received should still be accepted as no-op")
	}
	item = store.List(now)[0]
	if item.Deliveries["client"] != DeliveryDisplayed || item.DeliveryHistory["client"] != DeliveryDisplayed {
		t.Fatalf("late received regressed state: deliveries=%q history=%q", item.Deliveries["client"], item.DeliveryHistory["client"])
	}
}

func TestStoreExpiresSentMessageBeforeRetry(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	id, _ := NewMessageID()
	store := NewStore()
	if _, err := store.Create(Message{ID: id, ExpiresAt: now.Add(time.Minute).UnixMilli()}, []string{"client"}, now); err != nil {
		t.Fatal(err)
	}
	if store.NextPending(now) == nil || !store.MarkSent(id, "client", now) {
		t.Fatal("initial delivery was not marked sent")
	}
	store.Complete(id, now)
	if expired := store.Expire(now.Add(61 * time.Second)); expired != 1 {
		t.Fatalf("expired records = %d", expired)
	}
	item := store.List(now.Add(61 * time.Second))[0]
	if item.Status != DeliveryExpired || item.Deliveries["client"] != DeliveryExpired {
		t.Fatalf("sent message did not expire cleanly: %#v", item)
	}
	if got := store.QueueDueRetries(now.Add(time.Hour), time.Second); got != 0 {
		t.Fatalf("expired message was requeued: %d", got)
	}
}

func TestSQLiteDeliveryAttemptColumnUpgrade(t *testing.T) {
	path := filepath.Join(t.TempDir(), "legacy.db")
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`CREATE TABLE message_deliveries (message_id TEXT NOT NULL, client_id TEXT NOT NULL, status TEXT NOT NULL, PRIMARY KEY(message_id, client_id))`); err != nil {
		db.Close()
		t.Fatal(err)
	}
	_ = db.Close()
	store, err := NewPersistentStore(path)
	if err != nil {
		t.Fatal(err)
	}
	if store.db == nil {
		t.Fatal("expected SQLite backend")
	}
	var column string
	if err := store.db.QueryRow(`SELECT name FROM pragma_table_info('message_deliveries') WHERE name = 'last_attempt_at'`).Scan(&column); err != nil {
		t.Fatalf("last_attempt_at was not added: %v", err)
	}
}

func TestWildcardBroadcastDoesNotCreateStarDeliveryOrBusyRetry(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	store := NewStore()
	id, err := NewMessageID()
	if err != nil {
		t.Fatal(err)
	}
	record, err := store.Create(Message{ID: id, DisplayText: "broadcast"}, []string{"*"}, now)
	if err != nil {
		t.Fatal(err)
	}
	if _, exists := record.Deliveries["*"]; exists {
		t.Fatalf("wildcard marker must not become a delivery row: %#v", record.Deliveries)
	}
	if got := store.QueueDueRetries(now.Add(time.Second), 300*time.Second); got != 0 {
		t.Fatalf("wildcard-only message busy-retried: %d", got)
	}
	if added := store.AddClientToBroadcasts("client-a", now); added != 1 {
		t.Fatalf("AddClientToBroadcasts = %d, want 1", added)
	}
	items := store.List(now)
	if len(items) != 1 || items[0].Deliveries["client-a"] != DeliveryPending {
		t.Fatalf("expanded delivery missing: %#v", items)
	}
	if _, exists := items[0].Deliveries["*"]; exists {
		t.Fatalf("wildcard delivery row appeared after expand: %#v", items[0].Deliveries)
	}
	// AddClientToBroadcasts already requeues when the message is not queued.
	// Drain that queue entry, then confirm QueueDueRetries brings it back.
	if next := store.NextPending(now); next == nil || next.Message.ID != id {
		t.Fatalf("expanded broadcast was not queued: %#v", next)
	}
	if got := store.QueueDueRetries(now.Add(2*time.Second), 300*time.Second); got != 1 {
		t.Fatalf("pending real client should requeue once: %d", got)
	}
}

func TestSQLiteRetryTimestampSurvivesRestart(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	path := filepath.Join(t.TempDir(), "server.db")
	id, _ := NewMessageID()
	store, err := NewPersistentStore(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Create(Message{ID: id}, []string{"client"}, now); err != nil {
		t.Fatal(err)
	}
	if store.NextPending(now) == nil || !store.MarkSent(id, "client", now) {
		t.Fatal("initial delivery was not marked sent")
	}
	store.Complete(id, now)
	if !store.Acknowledge(id, "client", DeliveryReceived) {
		t.Fatal("received acknowledgement rejected")
	}
	if err := store.db.Close(); err != nil {
		t.Fatal(err)
	}
	restored, err := NewPersistentStore(path)
	if err != nil {
		t.Fatal(err)
	}
	items := restored.List(now)
	if len(items) != 1 || items[0].DeliveryUpdatedAt["client"] != now.UnixMilli() {
		t.Fatalf("delivery attempt timestamp was not restored: %#v", items)
	}
	if got := restored.QueueDueRetries(now.Add(299*time.Second), 300*time.Second); got != 0 {
		t.Fatalf("restored message retried before timeout: %d", got)
	}
	if got := restored.QueueDueRetries(now.Add(300*time.Second), 300*time.Second); got != 1 {
		t.Fatalf("restored message was not retried at timeout: %d", got)
	}
}
