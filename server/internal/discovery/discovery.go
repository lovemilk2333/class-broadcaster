package discovery

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"crypto/x509"
	"errors"
	"net"
	"time"

	"go.mongodb.org/mongo-driver/bson"

	"lovemilk-class-broadcaster/server/internal/identity"
	"lovemilk-class-broadcaster/server/internal/protocol"
)

const signatureDomain = "MKCB-DISCOVERY-V1\x00"

type Request struct {
	Nonce         []byte `bson:"nonce"`
	ProtocolMajor uint8  `bson:"protocol_major"`
	ProtocolMinor uint8  `bson:"protocol_minor"`
}

type Response struct {
	ServiceName     string `bson:"service_name"`
	ServerPublicKey []byte `bson:"server_public_key"`
	Fingerprint     []byte `bson:"fingerprint"`
	TCPPort         uint16 `bson:"tcp_port"`
	HTTPPort        uint16 `bson:"http_port"`
	ProtocolMajor   uint8  `bson:"protocol_major"`
	ProtocolMinor   uint8  `bson:"protocol_minor"`
	Nonce           []byte `bson:"nonce"`
	Signature       []byte `bson:"signature"`
}

type Responder struct {
	Identity    identity.Identity
	ServiceName string
	TCPPort     uint16
	HTTPPort    uint16
}

func (r Responder) Build(request Request, seq uint16) (protocol.Packet, error) {
	if len(request.Nonce) < 8 || len(request.Nonce) > 64 {
		return protocol.Packet{}, errors.New("discovery nonce must be 8-64 bytes")
	}
	response := Response{
		ServiceName:     r.ServiceName,
		ServerPublicKey: append([]byte(nil), r.Identity.PublicKey...),
		Fingerprint:     r.Identity.Fingerprint(),
		TCPPort:         r.TCPPort,
		HTTPPort:        r.HTTPPort,
		ProtocolMajor:   protocol.ProtocolMajor,
		ProtocolMinor:   protocol.ProtocolMinor,
		Nonce:           append([]byte(nil), request.Nonce...),
	}
	unsigned, err := unsignedPayload(response)
	if err != nil {
		return protocol.Packet{}, err
	}
	response.Signature = ed25519.Sign(r.Identity.PrivateKey, signingBytes(unsigned))
	payload, err := protocol.MarshalBSON(response)
	if err != nil {
		return protocol.Packet{}, err
	}
	return protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.DiscoveryResp, seq, 0, payload), nil
}

func Verify(response Response) error {
	if len(response.ServerPublicKey) != ed25519.PublicKeySize {
		return errors.New("invalid server public key")
	}
	if len(response.Fingerprint) != 32 {
		return errors.New("invalid server fingerprint")
	}
	if len(response.Signature) != ed25519.SignatureSize {
		return errors.New("invalid discovery signature")
	}
	unsigned, err := unsignedPayload(response)
	if err != nil {
		return err
	}
	if !ed25519.Verify(ed25519.PublicKey(response.ServerPublicKey), signingBytes(unsigned), response.Signature) {
		return errors.New("invalid discovery signature")
	}
	spki, err := x509.MarshalPKIXPublicKey(ed25519.PublicKey(response.ServerPublicKey))
	if err != nil {
		return err
	}
	want := sha256.Sum256(spki)
	if !bytes.Equal(response.Fingerprint, want[:]) {
		return errors.New("discovery fingerprint does not match public key")
	}
	if len(response.Nonce) < 8 || len(response.Nonce) > 64 {
		return errors.New("invalid discovery nonce")
	}
	return nil
}

func unsignedPayload(response Response) ([]byte, error) {
	response.Signature = nil
	return bson.Marshal(response)
}

func signingBytes(payload []byte) []byte {
	return append([]byte(signatureDomain), payload...)
}

func (r Responder) Serve(ctx context.Context, conn *net.UDPConn) error {
	// Discovery is UDP-only and tiny. Allocating MaxPayloadSize (hundreds of MiB)
	// here OOMs low-RAM devices (e.g. 256–512 MiB class boards) on startup.
	buf := make([]byte, protocol.MaxDiscoveryDatagram)
	for {
		if err := conn.SetReadDeadline(time.Now().Add(time.Second)); err != nil {
			return err
		}
		n, addr, err := conn.ReadFromUDP(buf)
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				return nil
			}
			if timeout, ok := err.(net.Error); ok && timeout.Timeout() {
				select {
				case <-ctx.Done():
					return nil
				default:
					continue
				}
			}
			return err
		}
		packet, err := protocol.ReadPacket(bytes.NewReader(buf[:n]))
		if err != nil || packet.Type != protocol.DiscoveryReq {
			continue
		}
		var request Request
		if err := protocol.UnmarshalBSON(packet.Payload, &request); err != nil {
			continue
		}
		response, err := r.Build(request, packet.Seq)
		if err != nil {
			continue
		}
		wire, err := response.MarshalBinary()
		if err != nil {
			continue
		}
		if _, err := conn.WriteToUDP(wire, addr); err != nil {
			return err
		}
	}
}
