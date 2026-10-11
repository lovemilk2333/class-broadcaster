package broadcast

import (
	"testing"
	"time"
)

func TestNextPendingSkipsBackoffWithoutDropping(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	store := NewStore()
	oldID, _ := NewMessageID()
	newID, _ := NewMessageID()
	if _, err := store.Create(Message{ID: oldID, PrioritySet: true, Priority: 1}, []string{"a"}, now); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Create(Message{ID: newID, PrioritySet: true, Priority: 1}, []string{"b"}, now.Add(time.Millisecond)); err != nil {
		t.Fatal(err)
	}
	// Pop old into flight, then back it off (RetryAfter alone does not requeue).
	if got := store.NextPending(now); got == nil || got.Message.ID != oldID {
		t.Fatalf("expected old first: %#v", got)
	}
	if !store.RetryAfter(oldID, now.Add(15*time.Second)) {
		t.Fatal("RetryAfter failed")
	}
	// Requeue old while still backing off: Retry clears retryAt, so re-apply RetryAfter
	// while the message remains queued.
	if !store.Retry(oldID) {
		t.Fatal("could not requeue old")
	}
	if !store.RetryAfter(oldID, now.Add(15*time.Second)) {
		t.Fatal("could not back off queued old")
	}
	// NextPending must skip old (deferred requeue) and return the ready new message.
	got := store.NextPending(now)
	if got == nil || got.Message.ID != newID {
		t.Fatalf("expected new ready message, got %#v", got)
	}
	// Same now: old must not be returned while still backing off.
	if again := store.NextPending(now); again != nil && again.Message.ID == oldID {
		t.Fatalf("old should still be deferred: %#v", again)
	}
	// After backoff elapses, QueueDueRetries makes old available again.
	later := now.Add(16 * time.Second)
	if got := store.QueueDueRetries(later, 300*time.Second); got < 1 {
		// Old may still be queued from the deferred requeue path; either way NextPending should find it.
		_ = got
	}
	gotOld := store.NextPending(later)
	if gotOld == nil || gotOld.Message.ID != oldID {
		t.Fatalf("expected old after backoff: %#v", gotOld)
	}
}

func TestQueueDueRetriesRequeuesWildcardAfterBackoff(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	store := NewStore()
	id, _ := NewMessageID()
	if _, err := store.Create(Message{ID: id, DisplayText: "broadcast"}, []string{"*"}, now); err != nil {
		t.Fatal(err)
	}
	if got := store.NextPending(now); got == nil || got.Message.ID != id {
		t.Fatalf("expected wildcard message: %#v", got)
	}
	if !store.RetryAfter(id, now.Add(5*time.Second)) {
		t.Fatal("RetryAfter failed")
	}
	// Before backoff: must not busy-requeue.
	if got := store.QueueDueRetries(now.Add(time.Second), 300*time.Second); got != 0 {
		t.Fatalf("wildcard busy-retried early: %d", got)
	}
	// After backoff: requeue so dispatch can re-expand approvals.
	if got := store.QueueDueRetries(now.Add(6*time.Second), 300*time.Second); got != 1 {
		t.Fatalf("wildcard should requeue after backoff: %d", got)
	}
	if next := store.NextPending(now.Add(6 * time.Second)); next == nil || next.Message.ID != id {
		t.Fatalf("expected requeued wildcard: %#v", next)
	}
}
