package broadcast

import (
	"path/filepath"
	"testing"
	"time"
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
	if len(items) != 2 || items[0].Message.QueueSeq == 0 || items[0].Status != DeliverySent {
		t.Fatalf("message history was not restored: %#v", items)
	}
	if next := restored.NextPending(now); next == nil || next.Message.ID != completedID {
		t.Fatalf("pending message was not requeued after restart: %#v", next)
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

func TestStorePrunesHistoryAfter48Hours(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	store := NewStore()
	oldID, _ := NewMessageID()
	newID, _ := NewMessageID()
	if _, err := store.Create(Message{ID: oldID}, nil, now); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Create(Message{ID: newID}, nil, now); err != nil {
		t.Fatal(err)
	}
	store.mu.Lock()
	old := store.records[oldID]
	old.CreatedAt = now.Add(-HistoryRetention)
	store.records[oldID] = old
	store.mu.Unlock()
	if removed := store.Expire(now); removed != 1 {
		t.Fatalf("removed = %d, want 1", removed)
	}
	items := store.List(now)
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
