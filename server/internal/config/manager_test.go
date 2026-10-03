package config

import (
	"testing"
	"time"
)

func TestManagerUpdateCreatesNewSnapshot(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	manager, err := NewManager(now)
	if err != nil {
		t.Fatal(err)
	}
	old := manager.Current()
	interval := 20 * time.Second
	ttl := 48 * time.Hour
	updated, err := manager.Update(Update{HeartbeatInterval: &interval, MessageTTL: &ttl}, now.Add(time.Second))
	if err != nil {
		t.Fatal(err)
	}
	if string(old.ID) == string(updated.ID) || updated.HeartbeatInterval != interval || updated.MessageTTL != ttl {
		t.Fatalf("unexpected update: %#v", updated)
	}
}

func TestManagerRejectsInvalidHeartbeat(t *testing.T) {
	manager, err := NewManager(time.Now())
	if err != nil {
		t.Fatal(err)
	}
	interval := 30 * time.Second
	timeout := 10 * time.Second
	if _, err := manager.Update(Update{HeartbeatInterval: &interval, HeartbeatTimeout: &timeout}, time.Now()); err == nil {
		t.Fatal("invalid heartbeat timeout accepted")
	}
}

func TestManagerPublishesOnlyWhenRequested(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	manager, err := NewManager(now)
	if err != nil {
		t.Fatal(err)
	}
	events, unsubscribe := manager.Subscribe()
	defer unsubscribe()
	interval := 20 * time.Second
	if _, err := manager.Update(Update{HeartbeatInterval: &interval}, now.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	select {
	case <-events:
		t.Fatal("update was published without immediate request")
	default:
	}
	manager.Publish(manager.Current())
	select {
	case event := <-events:
		if event.HeartbeatInterval != interval {
			t.Fatalf("unexpected event: %#v", event)
		}
	case <-time.After(time.Second):
		t.Fatal("configuration event was not published")
	}
}
