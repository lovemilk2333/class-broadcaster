package session

import (
	"errors"
	"net"
	"sync"

	"lovemilk-class-broadcaster/server/internal/protocol"
)

// Hub tracks authenticated client connections. A client ID can have only one
// active session; a reconnect replaces the previous connection.
type Hub struct {
	mu       sync.RWMutex
	sessions map[string]*Connection
}

type Connection struct {
	clientID string
	conn     net.Conn
	mu       sync.Mutex
}

func NewHub() *Hub { return &Hub{sessions: make(map[string]*Connection)} }

func (h *Hub) Register(clientID string, conn net.Conn) func() {
	session := &Connection{clientID: clientID, conn: conn}
	h.mu.Lock()
	previous := h.sessions[clientID]
	h.sessions[clientID] = session
	h.mu.Unlock()
	if previous != nil {
		_ = previous.conn.Close()
	}
	return func() {
		h.mu.Lock()
		if h.sessions[clientID] == session {
			delete(h.sessions, clientID)
		}
		h.mu.Unlock()
	}
}

// ErrClientOffline is returned when Send targets a client with no live TLS session.
var ErrClientOffline = errors.New("client is offline")

func (h *Hub) Send(clientID string, packet protocol.Packet) error {
	h.mu.RLock()
	session := h.sessions[clientID]
	h.mu.RUnlock()
	if session == nil {
		return ErrClientOffline
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	return packet.WritePacket(session.conn)
}

func (h *Hub) Online(clientID string) bool {
	h.mu.RLock()
	_, ok := h.sessions[clientID]
	h.mu.RUnlock()
	return ok
}

func (h *Hub) OnlineIDs() []string {
	h.mu.RLock()
	defer h.mu.RUnlock()
	ids := make([]string, 0, len(h.sessions))
	for id := range h.sessions {
		ids = append(ids, id)
	}
	return ids
}

// Disconnect closes an active session when its authorization is revoked.
func (h *Hub) Disconnect(clientID string) {
	h.mu.Lock()
	session := h.sessions[clientID]
	delete(h.sessions, clientID)
	h.mu.Unlock()
	if session != nil {
		_ = session.conn.Close()
	}
}

// SuspendClient tells the client to stop its automatic reconnect loop, then
// closes the session. A later explicit connection attempt remains possible.
func (h *Hub) SuspendClient(clientID string) {
	h.mu.Lock()
	session := h.sessions[clientID]
	delete(h.sessions, clientID)
	h.mu.Unlock()
	if session == nil {
		return
	}
	session.mu.Lock()
	_ = protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.ClientDisconnect, 0, 0, nil).WritePacket(session.conn)
	_ = session.conn.Close()
	session.mu.Unlock()
}
