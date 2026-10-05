package protocol

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"io"

	"go.mongodb.org/mongo-driver/bson"
)

const (
	Magic      = "MKCB"
	HeaderSize = 16
	// MaxPayloadSize bounds TLS/control packet payloads (messages, handshakes, updates).
	// Keep well under typical low-end device RAM; client update packages are ~100MB compressed.
	MaxPayloadSize = 128 << 20
	// MaxDiscoveryDatagram is the UDP read buffer for discovery only.
	// Discovery packets are tiny; never allocate MaxPayloadSize for UDP.
	MaxDiscoveryDatagram = 64 << 10
	ProtocolMajor        = 1
	ProtocolMinor        = 0
	DiscoveryReq     uint16 = 0x0001
	DiscoveryResp    uint16 = 0x0002
	ConnectReq       uint16 = 0x0101
	ConnectResp      uint16 = 0x0102
	ConfigUpdate     uint16 = 0x0103
	Ping             uint16 = 0x0201
	Pong             uint16 = 0x0202
	ServerShutdown   uint16 = 0x0203
	ClientDisconnect uint16 = 0x0205
	ClientLog          uint16 = 0x0206
	UpdateAvailable    uint16 = 0x0207
	UpdateDownloadReq  uint16 = 0x0208 // dedicated TLS session: request package by token
	UpdateDownloadResp uint16 = 0x0209 // chunked package bytes (or error)
	// ClientSessionEnd is a client→server goodbye before closing the TLS session.
	// Payload reason: user_exit | update | admin (optional detail).
	ClientSessionEnd uint16 = 0x020A
	ListenerPing     uint16 = 0x0204
	Message          uint16 = 0x1001
	MessageAck       uint16 = 0x1002
	MessageWithdraw  uint16 = 0x1003
	Error            uint16 = 0x7f00
)

// Update download chunk size keeps memory bounded on low-RAM hosts.
// Keep well under a single TLS application record cascade so slow clients can
// assemble frames without multi-second stalls (BSON overhead is a few hundred bytes).
const UpdateDownloadChunkSize = 256 << 10 // 256 KiB

var (
	ErrInvalidMagic    = errors.New("invalid MKCB magic")
	ErrInvalidLength   = errors.New("invalid packet length")
	ErrPayloadTooLarge = errors.New("packet payload exceeds limit")
)

// Packet is the wire representation used over both discovery and TLS streams.
type Packet struct {
	VersionMajor uint8
	VersionMinor uint8
	Type         uint16
	Seq          uint16
	Flags        uint16
	Payload      []byte
}

func (p Packet) MarshalBinary() ([]byte, error) {
	if len(p.Payload) > MaxPayloadSize {
		return nil, ErrPayloadTooLarge
	}
	length := HeaderSize + len(p.Payload)
	if uint64(length) > uint64(^uint32(0)) {
		return nil, ErrInvalidLength
	}

	out := make([]byte, length)
	copy(out[0:4], Magic)
	out[4] = p.VersionMajor
	out[5] = p.VersionMinor
	binary.BigEndian.PutUint16(out[6:8], p.Type)
	binary.BigEndian.PutUint32(out[8:12], uint32(length))
	binary.BigEndian.PutUint16(out[12:14], p.Seq)
	binary.BigEndian.PutUint16(out[14:16], p.Flags)
	copy(out[HeaderSize:], p.Payload)
	return out, nil
}

func (p Packet) WritePacket(w io.Writer) error {
	b, err := p.MarshalBinary()
	if err != nil {
		return err
	}
	for len(b) > 0 {
		n, writeErr := w.Write(b)
		if writeErr != nil {
			return writeErr
		}
		if n == 0 {
			return io.ErrShortWrite
		}
		b = b[n:]
	}
	return nil
}

func ReadPacket(r io.Reader) (Packet, error) {
	var header [HeaderSize]byte
	if _, err := io.ReadFull(r, header[:]); err != nil {
		return Packet{}, err
	}
	if string(header[0:4]) != Magic {
		return Packet{}, ErrInvalidMagic
	}
	length := binary.BigEndian.Uint32(header[8:12])
	if length < HeaderSize {
		return Packet{}, ErrInvalidLength
	}
	if length > HeaderSize+MaxPayloadSize {
		return Packet{}, ErrPayloadTooLarge
	}
	p := Packet{
		VersionMajor: header[4],
		VersionMinor: header[5],
		Type:         binary.BigEndian.Uint16(header[6:8]),
		Seq:          binary.BigEndian.Uint16(header[12:14]),
		Flags:        binary.BigEndian.Uint16(header[14:16]),
		Payload:      make([]byte, int(length)-HeaderSize),
	}
	if _, err := io.ReadFull(r, p.Payload); err != nil {
		return Packet{}, err
	}
	return p, nil
}

func MarshalBSON(v any) ([]byte, error) {
	return bson.Marshal(v)
}

func UnmarshalBSON(payload []byte, v any) error {
	if len(payload) == 0 {
		return errors.New("empty BSON payload")
	}
	return bson.Unmarshal(payload, v)
}

func New(versionMajor uint8, versionMinor uint8, typ uint16, seq uint16, flags uint16, payload []byte) Packet {
	return Packet{VersionMajor: versionMajor, VersionMinor: versionMinor, Type: typ, Seq: seq, Flags: flags, Payload: payload}
}

func (p Packet) String() string {
	return fmt.Sprintf("MKCB/%d.%d type=0x%04x seq=%d flags=0x%04x payload=%d", p.VersionMajor, p.VersionMinor, p.Type, p.Seq, p.Flags, len(p.Payload))
}

// BytesReader is useful in tests and small in-memory protocol adapters.
func BytesReader(p Packet) (io.Reader, error) {
	b, err := p.MarshalBinary()
	if err != nil {
		return nil, err
	}
	return bytes.NewReader(b), nil
}
