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
}

type ConnectResponse struct {
	RequestID       []byte           `bson:"request_id"`
	SessionID       []byte           `bson:"session_id"`
	ServerPublicKey []byte           `bson:"server_public_key"`
	ProtocolMajor   uint8            `bson:"protocol_major"`
	ProtocolMinor   uint8            `bson:"protocol_minor"`
	ConfigUpdated   bool             `bson:"config_updated"`
	Config          *config.Snapshot `bson:"config,omitempty"`
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
