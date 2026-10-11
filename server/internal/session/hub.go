package session

import (
	"errors"
	"net"
	"sync"
	"time"

	"lovemilk-class-broadcaster/server/internal/protocol"
)

// Hub tracks authenticated client connections. A client ID can have only one
// active session; a reconnect replaces the previous connection.
type Hub struct {
	mu         sync.RWMutex
	sessions   map[string]*Connection
	activities map[string]ClientActivity // ephemeral; cleared on disconnect / idle
}

// ClientActivity is a short-lived UI hint (not persisted).
type ClientActivity struct {
	State     string // processing | idle | ""
	MessageID string
	Detail    string
	ElapsedMS int64
	StartedAt int64 // unix ms when this processing episode began
	UpdatedAt int64 // unix ms of last report
}

type Connection struct {
	clientID string
	conn     net.Conn
	mu       sync.Mutex
}

// DefaultSendWriteTimeout bounds hub.Send so a half-dead TCP peer cannot stall
// the single-threaded message dispatcher (withdrawals use a separate goroutine
// and would still appear to "work" while broadcasts freeze).
const DefaultSendWriteTimeout = 5 * time.Second

func NewHub() *Hub {
	return &Hub{
		sessions:   make(map[string]*Connection),
		activities: make(map[string]ClientActivity),
	}
}

func (h *Hub) Register(clientID string, conn net.Conn) func() {
	session := &Connection{clientID: clientID, conn: conn}
	h.mu.Lock()
	previous := h.sessions[clientID]
	h.sessions[clientID] = session
	// Fresh session starts idle; drop any stale activity from a prior connection.
	delete(h.activities, clientID)
	h.mu.Unlock()
	if previous != nil {
		_ = previous.conn.Close()
	}
	return func() {
		h.mu.Lock()
		if h.sessions[clientID] == session {
			delete(h.sessions, clientID)
			delete(h.activities, clientID)
		}
		h.mu.Unlock()
	}
}

// SetActivity records an ephemeral client activity hint for the admin UI.
func (h *Hub) SetActivity(clientID string, activity ClientActivity) {
	if clientID == "" {
		return
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	now := time.Now().UnixMilli()
	if activity.UpdatedAt == 0 {
		activity.UpdatedAt = now
	}
	state := activity.State
	if state == "" || state == "idle" {
		delete(h.activities, clientID)
		return
	}
	if prev, ok := h.activities[clientID]; ok &&
		prev.State == "processing" &&
		prev.MessageID == activity.MessageID &&
		prev.StartedAt > 0 {
		activity.StartedAt = prev.StartedAt
	} else if activity.StartedAt == 0 {
		activity.StartedAt = now
	}
	h.activities[clientID] = activity
}

// Activity returns the last reported activity for an online client (zero value if none).
func (h *Hub) Activity(clientID string) ClientActivity {
	h.mu.RLock()
	defer h.mu.RUnlock()
	return h.activities[clientID]
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
	_ = session.conn.SetWriteDeadline(time.Now().Add(DefaultSendWriteTimeout))
	err := packet.WritePacket(session.conn)
	_ = session.conn.SetWriteDeadline(time.Time{}) // clear
	if err != nil {
		// Drop the stuck session so the dispatcher can move on; the client will reconnect.
		go h.Disconnect(clientID)
		return err
	}
	return nil
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
	delete(h.activities, clientID)
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
	delete(h.activities, clientID)
	h.mu.Unlock()
	if session == nil {
		return
	}
	session.mu.Lock()
	_ = protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.ClientDisconnect, 0, 0, nil).WritePacket(session.conn)
	_ = session.conn.Close()
	session.mu.Unlock()
}
