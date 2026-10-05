package session

import (
	"net"
	"testing"
	"time"

	"lovemilk-class-broadcaster/server/internal/protocol"
)

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
