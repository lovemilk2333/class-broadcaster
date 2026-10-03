package discovery

import (
	"bytes"
	"crypto/sha256"
	"crypto/x509"
	"testing"
	"time"

	"lovemilk-class-broadcaster/server/internal/identity"
	"lovemilk-class-broadcaster/server/internal/protocol"
)

func TestBuildAndVerify(t *testing.T) {
	id, err := identity.NewSelfSigned("test-server", time.Unix(1_700_000_000, 0))
	if err != nil {
		t.Fatal(err)
	}
	responder := Responder{Identity: id, ServiceName: "test", TCPPort: 39002, HTTPPort: 39003}
	request := Request{Nonce: []byte("0123456789abcdef"), ProtocolMajor: 1}
	packet, err := responder.Build(request, 7)
	if err != nil {
		t.Fatal(err)
	}
	if packet.Type != protocol.DiscoveryResp || packet.Seq != 7 {
		t.Fatalf("unexpected packet: %#v", packet)
	}
	var response Response
	if err := protocol.UnmarshalBSON(packet.Payload, &response); err != nil {
		t.Fatal(err)
	}
	if err := Verify(response); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(response.Nonce, request.Nonce) {
		t.Fatal("nonce was not echoed")
	}
	spki, err := x509.MarshalPKIXPublicKey(id.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	want := sha256.Sum256(spki)
	if !bytes.Equal(response.Fingerprint, want[:]) {
		t.Fatal("fingerprint does not match SPKI")
	}
}

func TestVerifyRejectsTampering(t *testing.T) {
	id, err := identity.NewSelfSigned("test-server", time.Now())
	if err != nil {
		t.Fatal(err)
	}
	packet, err := (Responder{Identity: id}).Build(Request{Nonce: []byte("01234567")}, 1)
	if err != nil {
		t.Fatal(err)
	}
	var response Response
	if err := protocol.UnmarshalBSON(packet.Payload, &response); err != nil {
		t.Fatal(err)
	}
	response.ServiceName = "tampered"
	if err := Verify(response); err == nil {
		t.Fatal("tampered response verified")
	}
	packet, err = (Responder{Identity: id}).Build(Request{Nonce: []byte("01234567")}, 1)
	if err != nil {
		t.Fatal(err)
	}
	if err := protocol.UnmarshalBSON(packet.Payload, &response); err != nil {
		t.Fatal(err)
	}
	response.Fingerprint[0] ^= 0xff
	if err := Verify(response); err == nil {
		t.Fatal("fingerprint tampering verified")
	}
}

func TestBuildRejectsInvalidNonce(t *testing.T) {
	id, err := identity.NewSelfSigned("test-server", time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := (Responder{Identity: id}).Build(Request{Nonce: []byte("short")}, 1); err == nil {
		t.Fatal("short nonce accepted")
	}
}
