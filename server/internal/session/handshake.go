package session

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"errors"
	"time"

	"lovemilk-class-broadcaster/server/internal/config"
	"lovemilk-class-broadcaster/server/internal/protocol"
)

const SessionIDSize = 16

var (
	ErrMissingClientKey  = errors.New("client public key is required")
	ErrClientKeyMismatch = errors.New("client public key does not match TLS certificate")
	ErrProtocolMismatch  = errors.New("unsupported protocol version")
)

type ConnectRequest struct {
	RequestID       []byte `bson:"request_id"`
	ClientPublicKey []byte `bson:"client_public_key"`
	ConfigID        string `bson:"config_id,omitempty"`
	ProtocolMajor   uint8  `bson:"protocol_major"`
	ProtocolMinor   uint8  `bson:"protocol_minor"`
	ListenerMode    string `bson:"listener_mode,omitempty"`
	ListenerPort    uint16 `bson:"listener_port,omitempty"`
	ReconnectProbe  bool   `bson:"reconnect_probe,omitempty"`
	ManualConnect   bool   `bson:"manual_connect,omitempty"`
	ClientVersion   string `bson:"client_version,omitempty"`
	// UpdateDownload marks a short-lived TLS session used only to fetch an update package.
	// The server accepts the handshake then serves UpdateDownloadReq/Resp and closes.
	UpdateDownload bool `bson:"update_download,omitempty"`
}

type ConnectResponse struct {
	RequestID       []byte           `bson:"request_id"`
	SessionID       []byte           `bson:"session_id"`
	ServerPublicKey []byte           `bson:"server_public_key"`
	ProtocolMajor   uint8            `bson:"protocol_major"`
	ProtocolMinor   uint8            `bson:"protocol_minor"`
	ConfigUpdated   bool             `bson:"config_updated"`
	Config          *config.Snapshot `bson:"config,omitempty"`
	ConnectionMode  string           `bson:"connection_mode,omitempty"`
	ConnectAllowed  bool             `bson:"connect_allowed"`
}

func ValidateRequest(request ConnectRequest, certificatePublicKey ed25519.PublicKey) error {
	if len(request.ClientPublicKey) != ed25519.PublicKeySize {
		return ErrMissingClientKey
	}
	if len(certificatePublicKey) != ed25519.PublicKeySize || !bytes.Equal(request.ClientPublicKey, certificatePublicKey) {
		return ErrClientKeyMismatch
	}
	if request.ProtocolMajor != protocol.ProtocolMajor {
		return ErrProtocolMismatch
	}
	return nil
}

// ReconnectAllowed applies server policy to periodic reconnect probes. Explicit
// manual attempts always remain possible for an approved client.
func ReconnectAllowed(mode string, probe, manual bool) bool {
	return !probe || manual || mode != "listen"
}

func BuildResponse(request ConnectRequest, serverPublicKey ed25519.PublicKey, snapshot config.Snapshot, now time.Time) (ConnectResponse, error) {
	if len(serverPublicKey) != ed25519.PublicKeySize {
		return ConnectResponse{}, errors.New("invalid server public key")
	}
	var sessionID [SessionIDSize]byte
	if _, err := rand.Read(sessionID[:]); err != nil {
		return ConnectResponse{}, err
	}
	refresh := config.NeedsRefresh(request.ConfigID, now, config.DefaultConfigMaxAge) || request.ConfigID != snapshot.ID
	response := ConnectResponse{
		RequestID:       append([]byte(nil), request.RequestID...),
		SessionID:       sessionID[:],
		ServerPublicKey: append([]byte(nil), serverPublicKey...),
		ProtocolMajor:   protocol.ProtocolMajor,
		ProtocolMinor:   protocol.ProtocolMinor,
		ConfigUpdated:   refresh,
		// Every connection needs the current connection-scoped heartbeat and
		// delivery settings, even when the caller's persisted snapshot ID matches.
		Config: &snapshot,
	}
	return response, nil
}
