package identity

import (
	"crypto/ed25519"
	"crypto/x509"
	"testing"
	"time"
)

func TestSelfSignedIdentity(t *testing.T) {
	id, err := NewSelfSigned("MKCB server", time.Unix(1_700_000_000, 0))
	if err != nil {
		t.Fatal(err)
	}
	if len(id.PublicKey) != ed25519.PublicKeySize || len(id.PrivateKey) != ed25519.PrivateKeySize {
		t.Fatalf("unexpected Ed25519 key sizes: %d/%d", len(id.PublicKey), len(id.PrivateKey))
	}
	if len(id.Fingerprint()) != 32 {
		t.Fatalf("fingerprint length = %d", len(id.Fingerprint()))
	}
	if _, err := id.TLSCertificate(); err != nil {
		t.Fatal(err)
	}
	parsed, err := x509.ParseCertificate(id.Certificate.Raw)
	if err != nil || !parsed.Equal(id.Certificate) {
		t.Fatalf("certificate parse failed: %v", err)
	}
	message := []byte("mkcb")
	signature := ed25519.Sign(id.PrivateKey, message)
	if !ed25519.Verify(id.PublicKey, message, signature) {
		t.Fatal("identity signature did not verify")
	}
}

func TestLoadOrCreatePersistsIdentity(t *testing.T) {
	dir := t.TempDir()
	first, err := LoadOrCreate(dir, "MKCB server", time.Unix(1_700_000_000, 0))
	if err != nil {
		t.Fatal(err)
	}
	second, err := LoadOrCreate(dir, "MKCB server", time.Unix(1_800_000_000, 0))
	if err != nil {
		t.Fatal(err)
	}
	if string(first.PublicKey) != string(second.PublicKey) || string(first.Fingerprint()) != string(second.Fingerprint()) {
		t.Fatal("persisted identity changed across reload")
	}
}
