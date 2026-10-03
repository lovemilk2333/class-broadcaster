package protocol

import (
	"bytes"
	"errors"
	"testing"
)

func TestPacketRoundTrip(t *testing.T) {
	want := New(ProtocolMajor, ProtocolMinor, Message, 42, 3, []byte{1, 2, 3})
	b, err := want.MarshalBinary()
	if err != nil {
		t.Fatal(err)
	}
	if got := len(b); got != HeaderSize+3 {
		t.Fatalf("packet length = %d, want %d", got, HeaderSize+3)
	}
	got, err := ReadPacket(bytes.NewReader(b))
	if err != nil {
		t.Fatal(err)
	}
	if got.VersionMajor != want.VersionMajor || got.VersionMinor != want.VersionMinor || got.Type != want.Type || got.Seq != want.Seq || got.Flags != want.Flags || !bytes.Equal(got.Payload, want.Payload) {
		t.Fatalf("round trip mismatch: got %#v want %#v", got, want)
	}
}

func TestPacketRejectsInvalidInput(t *testing.T) {
	badMagic := make([]byte, HeaderSize)
	copy(badMagic, "NOPE")
	if _, err := ReadPacket(bytes.NewReader(badMagic)); !errors.Is(err, ErrInvalidMagic) {
		t.Fatalf("invalid magic error = %v", err)
	}
	badLength := make([]byte, HeaderSize)
	copy(badLength, Magic)
	badLength[11] = 15
	if _, err := ReadPacket(bytes.NewReader(badLength)); !errors.Is(err, ErrInvalidLength) {
		t.Fatalf("invalid length error = %v", err)
	}
	if _, err := (Packet{Payload: make([]byte, MaxPayloadSize+1)}).MarshalBinary(); !errors.Is(err, ErrPayloadTooLarge) {
		t.Fatalf("oversize error = %v", err)
	}
}

func TestBSONRoundTrip(t *testing.T) {
	type payload struct {
		RequestID string `bson:"request_id"`
		Count     int32  `bson:"count"`
	}
	want := payload{RequestID: "abc", Count: 3}
	b, err := MarshalBSON(want)
	if err != nil {
		t.Fatal(err)
	}
	var got payload
	if err := UnmarshalBSON(b, &got); err != nil {
		t.Fatal(err)
	}
	if got != want {
		t.Fatalf("BSON mismatch: got %#v want %#v", got, want)
	}
}
