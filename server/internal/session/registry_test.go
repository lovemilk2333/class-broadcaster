package session

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"path/filepath"
	"testing"
	"time"
)

func TestRegistryApproval(t *testing.T) {
	publicKey, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	registry := NewRegistry()
	if registry.IsApproved(publicKey) {
		t.Fatal("unknown device is approved")
	}
	pending, err := registry.ObservePending(publicKey, time.Unix(1_700_000_000, 0))
	if err != nil || pending.Status != "pending" || registry.IsApproved(publicKey) {
		t.Fatalf("unknown device was not recorded as pending: %#v %v", pending, err)
	}
	device, err := registry.Approve(publicKey, "screen", time.Unix(1_700_000_000, 0))
	if err != nil || device.Label != "screen" {
		t.Fatalf("approval failed: %#v %v", device, err)
	}
	if !registry.IsApproved(publicKey) || len(registry.List()) != 1 {
		t.Fatal("approved device missing")
	}
	spki, err := x509.MarshalPKIXPublicKey(publicKey)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(spki)
	if device.ClientID != hex.EncodeToString(digest[:]) {
		t.Fatalf("client id is not the SPKI SHA-256 fingerprint: %s", device.ClientID)
	}
	encoded := "SHA256:" + base64.RawStdEncoding.EncodeToString(digest[:])
	if normalized, err := NormalizeClientID(encoded); err != nil || normalized != device.ClientID {
		t.Fatalf("base64 fingerprint was not normalized: %q %v", normalized, err)
	}
}

func TestRegistryListenerCapabilityAndForcedMode(t *testing.T) {
	publicKey, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Unix(1_700_000_000, 0)
	registry := NewRegistry()
	if _, err := registry.Approve(publicKey, "screen", now); err != nil {
		t.Fatal(err)
	}
	registry.UpdateListenerCapability(publicKey, "auto", 39004, "127.0.0.1", now)
	device := registry.List()[0]
	if device.RequestedMode != "auto" || device.ListenerPort != 39004 || device.ProbeUntil == 0 {
		t.Fatalf("listener capability was not recorded: %#v", device)
	}
	registry.RecordListenerProbe(device.ClientID, true, 12*time.Millisecond, now.Add(13*time.Hour))
	device = registry.List()[0]
	if device.ConnectionMode != "listen" || device.ProbeReceived != 1 || device.ProbeLossPercent != 0 {
		t.Fatalf("successful listener probe did not enable listen mode: %#v", device)
	}
	if err := registry.SetForcedMode(device.ClientID, "pull"); err != nil {
		t.Fatal(err)
	}
	device = registry.List()[0]
	if device.ForcedMode != "pull" || device.ConnectionMode != "pull" {
		t.Fatalf("forced pull mode was not persisted in memory: %#v", device)
	}
}

// TestRegistryCustomListenerPolicyUsesThresholdAndReset 验证自定义试用窗口、丢包阈值和重测间隔。
func TestRegistryCustomListenerPolicyUsesThresholdAndReset(t *testing.T) {
	publicKey, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Unix(1_700_000_000, 0)
	registry := NewRegistry()
	registry.SetListenerPolicy(ListenerPolicy{
		ProbeInterval: 17 * time.Second,
		ProbeDuration: 2 * time.Hour,
		ProbeReset:    3 * 24 * time.Hour,
		LossThreshold: 50,
		IdleTimeout:   9 * time.Second,
	})
	device, err := registry.Approve(publicKey, "screen", now)
	if err != nil {
		t.Fatal(err)
	}
	registry.UpdateListenerCapability(publicKey, "auto", 39004, "127.0.0.1", now)
	device, err = registry.Get(device.ClientID)
	if err != nil || device.ProbeUntil != now.Add(2*time.Hour).UnixMilli() {
		t.Fatalf("custom probe duration was not used: %#v %v", device, err)
	}
	registry.RecordListenerProbe(device.ClientID, true, 10*time.Millisecond, now.Add(time.Hour))
	registry.RecordListenerProbe(device.ClientID, false, 0, now.Add(3*time.Hour))
	device, _ = registry.Get(device.ClientID)
	if device.ConnectionMode != "listen" || device.ProbeUntil != 0 || device.ProbeLossPercent != 50 || device.ProbeReceived != 1 {
		t.Fatalf("custom loss threshold was not used: %#v", device)
	}
	registry.UpdateListenerCapability(publicKey, "auto", 39004, "127.0.0.1", now.Add(14*time.Hour))
	device, _ = registry.Get(device.ClientID)
	if device.ProbeUntil != 0 {
		t.Fatalf("successful listener trial was unexpectedly restarted: %#v", device)
	}

	// 新一轮试用使用更严格阈值，失败后应进入自定义重测等待期。
	registry.SetListenerPolicy(ListenerPolicy{
		ProbeInterval: 17 * time.Second,
		ProbeDuration: time.Hour,
		ProbeReset:    3 * 24 * time.Hour,
		LossThreshold: 0,
		IdleTimeout:   9 * time.Second,
	})
	if err := registry.SetForcedMode(device.ClientID, "pull"); err != nil {
		t.Fatal(err)
	}
	registry.UpdateListenerCapability(publicKey, "pull", 39004, "127.0.0.1", now.Add(4*time.Hour))
	registry.SetForcedMode(device.ClientID, "")
	registry.UpdateListenerCapability(publicKey, "auto", 39004, "127.0.0.1", now.Add(4*time.Hour))
	device, _ = registry.Get(device.ClientID)
	if device.ProbeUntil == 0 {
		t.Fatalf("new probe window was not started: %#v", device)
	}
	registry.RecordListenerProbe(device.ClientID, false, 0, now.Add(6*time.Hour))
	device, _ = registry.Get(device.ClientID)
	if device.ConnectionMode != "pull" || device.ProbeResetAt != now.Add(6*time.Hour).Add(3*24*time.Hour).UnixMilli() {
		t.Fatalf("custom reset interval was not used: %#v", device)
	}
	if policy := registry.ListenerPolicy(); policy.IdleTimeout != 9*time.Second || policy.ProbeInterval != 17*time.Second {
		t.Fatalf("custom listener policy was not retained: %#v", policy)
	}
}

func TestForcedListenRequiresAdvertisedListenerPort(t *testing.T) {
	publicKey, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	registry := NewRegistry()
	device, err := registry.Approve(publicKey, "screen", now)
	if err != nil {
		t.Fatal(err)
	}
	if err := registry.SetForcedMode(device.ClientID, "listen"); err != nil {
		t.Fatal(err)
	}
	registry.UpdateListenerCapability(publicKey, "pull", 0, "127.0.0.1", now)
	device, err = registry.Get(device.ClientID)
	if err != nil {
		t.Fatal(err)
	}
	if device.ConnectionMode != "pull" {
		t.Fatalf("listen mode reported without a client listener: %#v", device)
	}
	registry.UpdateListenerCapability(publicKey, "listen", 39004, "127.0.0.1", now)
	device, _ = registry.Get(device.ClientID)
	if device.ConnectionMode != "listen" {
		t.Fatalf("listen mode not applied when client advertised port: %#v", device)
	}
}

func TestSessionEndUserExitAndUnexpectedGrace(t *testing.T) {
	publicKey, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Unix(1_700_000_000, 0)
	registry := NewRegistry()
	device, err := registry.Approve(publicKey, "room-a", now)
	if err != nil {
		t.Fatal(err)
	}
	// Intentional exit → 人为终止 immediately.
	registry.RecordIntentionalSessionEnd(device.ClientID, SessionEndUserExit, "developer_exit", now)
	got, _ := registry.Get(device.ClientID)
	if got.SessionEndReason != SessionEndUserExit {
		t.Fatalf("user exit reason = %q", got.SessionEndReason)
	}
	reason, label := SessionEndDisplay(got, false, now)
	if reason != SessionEndUserExit || label != "人为终止" {
		t.Fatalf("display = %q %q", reason, label)
	}

	// Clear on reconnect.
	registry.ClearSessionEnd(device.ClientID, now.Add(time.Second))
	got, _ = registry.Get(device.ClientID)
	if got.SessionEndReason != SessionEndNone || got.SessionEndAt != 0 {
		t.Fatalf("clear failed: %#v", got)
	}

	// Unexplained drop → grace → unexpected after 30s.
	dropAt := now.Add(2 * time.Second)
	registry.NoteUnexpectedDisconnect(device.ClientID, dropAt)
	got, _ = registry.Get(device.ClientID)
	reason, label = SessionEndDisplay(got, false, dropAt.Add(10*time.Second))
	if reason != "reconnecting" || label != "重连中" {
		t.Fatalf("grace display = %q %q", reason, label)
	}
	online := map[string]struct{}{}
	if n := registry.FinalizeUnexpectedDisconnects(online, dropAt.Add(29*time.Second)); n != 0 {
		t.Fatalf("finalized too early: %d", n)
	}
	if n := registry.FinalizeUnexpectedDisconnects(online, dropAt.Add(30*time.Second)); n != 1 {
		t.Fatalf("expected 1 unexpected, got %d", n)
	}
	got, _ = registry.Get(device.ClientID)
	if got.SessionEndReason != SessionEndUnexpected {
		t.Fatalf("unexpected reason = %q", got.SessionEndReason)
	}
	reason, label = SessionEndDisplay(got, false, dropAt.Add(31*time.Second))
	if reason != SessionEndUnexpected || label != "意外终止" {
		t.Fatalf("unexpected display = %q %q", reason, label)
	}

	// Online client must not be finalized.
	registry.ClearSessionEnd(device.ClientID, dropAt.Add(time.Minute))
	registry.NoteUnexpectedDisconnect(device.ClientID, dropAt.Add(time.Minute))
	online[device.ClientID] = struct{}{}
	if n := registry.FinalizeUnexpectedDisconnects(online, dropAt.Add(2*time.Minute)); n != 0 {
		t.Fatalf("online client finalized: %d", n)
	}
}

func TestApprovedClientIDsExcludesPendingClients(t *testing.T) {
	approvedKey, _, _ := ed25519.GenerateKey(rand.Reader)
	pendingKey, _, _ := ed25519.GenerateKey(rand.Reader)
	registry := NewRegistry()
	approved, err := registry.Approve(approvedKey, "approved", time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := registry.ObservePending(pendingKey, time.Now()); err != nil {
		t.Fatal(err)
	}
	ids := registry.ApprovedClientIDs()
	if len(ids) != 1 || ids[0] != approved.ClientID {
		t.Fatalf("approved client IDs = %#v", ids)
	}
}

func TestRevokedClientRemainsRegisteredAndCanBeReapproved(t *testing.T) {
	publicKey, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	registry := NewRegistry()
	approved, err := registry.Approve(publicKey, "class display", now)
	if err != nil {
		t.Fatal(err)
	}
	if err := registry.RevokeClientID(approved.ClientID); err != nil {
		t.Fatal(err)
	}
	if registry.IsApproved(publicKey) {
		t.Fatal("revoked client remains authorized")
	}
	device, err := registry.Get(approved.ClientID)
	if err != nil || device.Status != "revoked" || device.Label != "class display" {
		t.Fatalf("revoked client record was not retained: %#v %v", device, err)
	}
	if _, err := registry.ObservePending(publicKey, now.Add(time.Minute)); err != nil {
		t.Fatal(err)
	}
	device, _ = registry.Get(approved.ClientID)
	if device.Status != "revoked" {
		t.Fatalf("revoked client became pending on reconnect: %#v", device)
	}
	device, err = registry.Approve(publicKey, "renamed display", now.Add(2*time.Minute))
	if err != nil || device.Status != "approved" || device.Label != "renamed display" || device.ApprovedAt != approved.ApprovedAt {
		t.Fatalf("revoked record was not reapproved in place: %#v %v", device, err)
	}
}

func TestRegistryPersistsPendingAndRevokedDevicesAcrossRestart(t *testing.T) {
	database := filepath.Join(t.TempDir(), "server.db")
	publicKey, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	registry, err := NewPersistentRegistry(database)
	if err != nil {
		t.Fatal(err)
	}
	pending, err := registry.ObservePending(publicKey, now)
	if err != nil {
		t.Fatal(err)
	}
	if err := registry.db.Close(); err != nil {
		t.Fatal(err)
	}

	registry, err = NewPersistentRegistry(database)
	if err != nil {
		t.Fatal(err)
	}
	restored, err := registry.Get(pending.ClientID)
	if err != nil || restored.Status != "pending" {
		t.Fatalf("pending client was not restored after restart: %#v %v", restored, err)
	}
	if _, err := registry.Approve(publicKey, "persisted", now); err != nil {
		t.Fatal(err)
	}
	registry.UpdateClientVersion(publicKey, "0.1.1")
	if err := registry.RevokeClientID(pending.ClientID); err != nil {
		t.Fatal(err)
	}
	if err := registry.db.Close(); err != nil {
		t.Fatal(err)
	}

	registry, err = NewPersistentRegistry(database)
	if err != nil {
		t.Fatal(err)
	}
	defer registry.db.Close()
	restored, err = registry.Get(pending.ClientID)
	if err != nil || restored.Status != "revoked" || restored.Label != "persisted" || restored.ClientVersion != "0.1.1" {
		t.Fatalf("revoked client was not restored after restart: %#v %v", restored, err)
	}
}

func TestRegistryPurgeStaleDevices(t *testing.T) {
	registry := NewRegistry()
	oldKey, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	freshKey, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Unix(1_700_000_000, 0)
	old, err := registry.Approve(oldKey, "old", now.Add(-DeviceRetention-time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	// Simulate long idle: last_seen also older than retention.
	registry.mu.Lock()
	device := registry.devices[old.ClientID]
	device.LastSeen = now.Add(-DeviceRetention - time.Hour).UnixMilli()
	registry.devices[old.ClientID] = device
	registry.mu.Unlock()
	fresh, err := registry.Approve(freshKey, "fresh", now)
	if err != nil {
		t.Fatal(err)
	}
	if removed := registry.PurgeStaleDevices(now, DeviceRetention); removed != 1 {
		t.Fatalf("removed = %d, want 1", removed)
	}
	if _, err := registry.Get(old.ClientID); err == nil {
		t.Fatal("stale device was not purged")
	}
	if _, err := registry.Get(fresh.ClientID); err != nil {
		t.Fatalf("fresh device was purged: %v", err)
	}
}

func TestRegistryListOrderIsStable(t *testing.T) {
	registry := NewRegistry()
	now := time.Unix(1_700_000_000, 0)
	// Distinct approved_at; last_seen deliberately inverted so default order
	// must ignore heartbeat churn.
	type fixture struct {
		label    string
		approved time.Time
		seen     time.Time
	}
	fixtures := []fixture{
		{label: "oldest", approved: now, seen: now.Add(3 * time.Hour)},
		{label: "mid", approved: now.Add(time.Minute), seen: now.Add(time.Hour)},
		{label: "newest", approved: now.Add(2 * time.Minute), seen: now},
	}
	for _, fx := range fixtures {
		publicKey, _, err := ed25519.GenerateKey(rand.Reader)
		if err != nil {
			t.Fatal(err)
		}
		device, err := registry.Approve(publicKey, fx.label, fx.approved)
		if err != nil {
			t.Fatal(err)
		}
		registry.mu.Lock()
		item := registry.devices[device.ClientID]
		item.LastSeen = fx.seen.UnixMilli()
		registry.devices[device.ClientID] = item
		registry.mu.Unlock()
	}
	// Pending must sort above all approved (even with approved_at=0 / old last_seen).
	pendingKey, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	pending, err := registry.ObservePending(pendingKey, now.Add(-time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	first := registry.List()
	if len(first) != 4 {
		t.Fatalf("len=%d", len(first))
	}
	// Pending first, then newest approved_at (not last_seen).
	if first[0].ClientID != pending.ClientID || first[0].Status != "pending" {
		t.Fatalf("want pending first, got %#v", first[0])
	}
	if first[1].Label != "newest" || first[2].Label != "mid" || first[3].Label != "oldest" {
		t.Fatalf("want approved_at desc labels [newest mid oldest], got %#v", []string{first[1].Label, first[2].Label, first[3].Label})
	}
	// Bump last_seen on the oldest approved device — order must not change.
	registry.mu.Lock()
	old := registry.devices[first[3].ClientID]
	old.LastSeen = now.Add(24 * time.Hour).UnixMilli()
	registry.devices[first[3].ClientID] = old
	registry.mu.Unlock()
	for round := 0; round < 20; round++ {
		again := registry.List()
		for i := range first {
			if again[i].ClientID != first[i].ClientID {
				t.Fatalf("order changed on refresh round=%d want=%s got=%s", round, first[i].ClientID, again[i].ClientID)
			}
		}
	}
}
