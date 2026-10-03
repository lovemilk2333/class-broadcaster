package session

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
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
