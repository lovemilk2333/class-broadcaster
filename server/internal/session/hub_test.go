package session

import (
	"net"
	"testing"
	"time"

	"lovemilk-class-broadcaster/server/internal/protocol"
)

func TestClientActivityClearedOnIdleAndDisconnect(t *testing.T) {
	hub := NewHub()
	serverConn, clientConn := net.Pipe()
	unregister := hub.Register("client", serverConn)
	defer func() {
		unregister()
		_ = clientConn.Close()
	}()

	hub.SetActivity("client", ClientActivity{State: "processing", MessageID: "m1", Detail: "hello"})
	got := hub.Activity("client")
	if got.State != "processing" || got.MessageID != "m1" || got.StartedAt == 0 {
		t.Fatalf("activity=%+v", got)
	}
	started := got.StartedAt
	hub.SetActivity("client", ClientActivity{State: "processing", MessageID: "m1", ElapsedMS: 1200})
	got = hub.Activity("client")
	if got.StartedAt != started {
		t.Fatalf("StartedAt reset on same-message refresh: %d → %d", started, got.StartedAt)
	}
	hub.SetActivity("client", ClientActivity{State: "idle"})
	if hub.Activity("client").State != "" {
		t.Fatal("idle did not clear activity")
	}

	hub.SetActivity("client", ClientActivity{State: "processing", MessageID: "m2"})
	hub.Disconnect("client")
	if hub.Activity("client").State != "" {
		t.Fatal("disconnect did not clear activity")
	}
}

func TestSuspendClientSendsDisconnectBeforeClosingSession(t *testing.T) {
	hub := NewHub()
	serverConn, clientConn := net.Pipe()
	hub.Register("client", serverConn)
	done := make(chan struct{})
	go func() {
		hub.SuspendClient("client")
		close(done)
	}()
	_ = clientConn.SetReadDeadline(time.Now().Add(time.Second))
	packet, err := protocol.ReadPacket(clientConn)
	if err != nil {
		t.Fatalf("read disconnect packet: %v", err)
	}
	if packet.Type != protocol.ClientDisconnect {
		t.Fatalf("packet type=0x%04x, want 0x%04x", packet.Type, protocol.ClientDisconnect)
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("suspend did not close the connection")
	}
	if hub.Online("client") {
		t.Fatal("suspended client remains online")
	}
	_ = clientConn.Close()
}
