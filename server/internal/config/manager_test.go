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

// TestManagerAcceptsCustomListenerPolicy 验证监听策略可以随配置快照发布并恢复。
func TestManagerAcceptsCustomListenerPolicy(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	manager, err := NewManager(now)
	if err != nil {
		t.Fatal(err)
	}
	interval := 17 * time.Second
	duration := 2 * time.Hour
	reset := 3 * 24 * time.Hour
	loss := int32(35)
	idle := 9 * time.Second
	updated, err := manager.Update(Update{
		ListenerProbeInterval: &interval,
		ListenerProbeDuration: &duration,
		ListenerProbeReset:    &reset,
		ListenerLossThreshold: &loss,
		ListenerIdleTimeout:   &idle,
	}, now.Add(time.Second))
	if err != nil {
		t.Fatal(err)
	}
	if updated.ListenerProbeInterval != interval || updated.ListenerProbeDuration != duration ||
		updated.ListenerProbeReset != reset || updated.ListenerLossThreshold != loss ||
		updated.ListenerIdleTimeout != idle {
		t.Fatalf("custom listener policy was not applied: %#v", updated)
	}
	restored, err := NewManagerFromSnapshot(updated)
	if err != nil {
		t.Fatal(err)
	}
	if got := restored.Current(); got.ListenerProbeInterval != interval || got.ListenerProbeDuration != duration ||
		got.ListenerProbeReset != reset || got.ListenerLossThreshold != loss || got.ListenerIdleTimeout != idle {
		t.Fatalf("custom listener policy was not restorable: %#v", got)
	}
}

// TestManagerRejectsInvalidListenerPolicy 保证边界值不会进入已发布快照。
func TestManagerRejectsInvalidListenerPolicy(t *testing.T) {
	manager, err := NewManager(time.Now())
	if err != nil {
		t.Fatal(err)
	}
	tooShort := 500 * time.Millisecond
	if _, err := manager.Update(Update{ListenerProbeInterval: &tooShort}, time.Now()); err == nil {
		t.Fatal("sub-second listener probe interval was accepted")
	}
	tooLong := 8 * 24 * time.Hour
	if _, err := manager.Update(Update{ListenerProbeDuration: &tooLong}, time.Now()); err == nil {
		t.Fatal("listener probe duration over seven days was accepted")
	}
}
