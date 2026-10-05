package session

import (
	"crypto/ed25519"
	"crypto/rand"
	"testing"
	"time"

	"lovemilk-class-broadcaster/server/internal/config"
)

func TestValidateRequestBindsCertificateKey(t *testing.T) {
	publicKey, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	request := ConnectRequest{ClientPublicKey: append([]byte(nil), publicKey...), ProtocolMajor: 1}
	if err := ValidateRequest(request, publicKey); err != nil {
		t.Fatal(err)
	}
	request.ClientPublicKey[0] ^= 1
	if err := ValidateRequest(request, publicKey); err != ErrClientKeyMismatch {
		t.Fatalf("error = %v", err)
	}
}

func TestBuildResponseRefreshesSnapshot(t *testing.T) {
	serverKey, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Unix(1_700_000_000, 0)
	snapshot, err := config.NewSnapshot(now)
	if err != nil {
		t.Fatal(err)
	}
	response, err := BuildResponse(ConnectRequest{RequestID: []byte("req")}, serverKey, snapshot, now)
	if err != nil {
		t.Fatal(err)
	}
	if !response.ConfigUpdated || response.Config == nil || len(response.SessionID) != SessionIDSize {
		t.Fatalf("unexpected response: %#v", response)
	}
	response, err = BuildResponse(ConnectRequest{ConfigID: snapshot.ID}, serverKey, snapshot, now)
	if err != nil {
		t.Fatal(err)
	}
	if response.ConfigUpdated || response.Config == nil || response.Config.ID != snapshot.ID {
		t.Fatal("matching config should include its connection snapshot without marking it updated")
	}
}

func TestReconnectAllowedUsesServerModeAndManualOverride(t *testing.T) {
	if !ReconnectAllowed("pull", true, false) {
		t.Fatal("pull mode should accept a periodic reconnect probe")
	}
	if ReconnectAllowed("listen", true, false) {
		t.Fatal("listen mode should decline an automatic reconnect probe")
	}
	if !ReconnectAllowed("listen", true, true) {
		t.Fatal("manual connection should override listen-mode probe policy")
	}
	if !ReconnectAllowed("listen", false, false) {
		t.Fatal("ordinary initial connections should remain allowed")
	}
}
