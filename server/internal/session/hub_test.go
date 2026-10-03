package session

import (
	"net"
	"testing"

	"lovemilk-class-broadcaster/server/internal/protocol"
)

func TestHubSendsToRegisteredConnection(t *testing.T) {
	left, right := net.Pipe()
	defer left.Close()
	defer right.Close()
	hub := NewHub()
	unregister := hub.Register("client", left)
	defer unregister()
	done := make(chan error, 1)
	go func() {
		_, err := protocol.ReadPacket(right)
		done <- err
	}()
	if err := hub.Send("client", protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.Pong, 1, 0, nil)); err != nil {
		t.Fatal(err)
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

func TestHubSendOfflineFails(t *testing.T) {
	if err := NewHub().Send("missing", protocol.Packet{}); err == nil {
		t.Fatal("expected offline error")
	}
}
