package identity

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"errors"
	"math/big"
	"os"
	"path/filepath"
	"time"
)

type Identity struct {
	PrivateKey  ed25519.PrivateKey
	PublicKey   ed25519.PublicKey
	Certificate *x509.Certificate
	CertPEM     []byte
	KeyPEM      []byte
}

func LoadOrCreate(dir, commonName string, now time.Time) (Identity, error) {
	certPEM, certErr := os.ReadFile(filepath.Join(dir, "server-cert.pem"))
	keyPEM, keyErr := os.ReadFile(filepath.Join(dir, "server-key.pem"))
	if certErr == nil && keyErr == nil {
		pair, err := tls.X509KeyPair(certPEM, keyPEM)
		if err != nil {
			return Identity{}, err
		}
		if len(pair.Certificate) != 1 {
			return Identity{}, errors.New("server certificate chain must contain one certificate")
		}
		certificate, err := x509.ParseCertificate(pair.Certificate[0])
		if err != nil {
			return Identity{}, err
		}
		privateKey, ok := pair.PrivateKey.(ed25519.PrivateKey)
		if !ok {
			return Identity{}, errors.New("server private key is not Ed25519")
		}
		return Identity{PrivateKey: privateKey, PublicKey: privateKey.Public().(ed25519.PublicKey), Certificate: certificate, CertPEM: certPEM, KeyPEM: keyPEM}, nil
	}
	if !errors.Is(certErr, os.ErrNotExist) || !errors.Is(keyErr, os.ErrNotExist) {
		return Identity{}, errors.New("read persisted server identity")
	}
	id, err := NewSelfSigned(commonName, now)
	if err != nil {
		return Identity{}, err
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return Identity{}, err
	}
	if err := os.WriteFile(filepath.Join(dir, "server-cert.pem"), id.CertPEM, 0o600); err != nil {
		return Identity{}, err
	}
	if err := os.WriteFile(filepath.Join(dir, "server-key.pem"), id.KeyPEM, 0o600); err != nil {
		return Identity{}, err
	}
	return id, nil
}

func NewSelfSigned(commonName string, now time.Time) (Identity, error) {
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return Identity{}, err
	}
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	if err != nil {
		return Identity{}, err
	}
	template := &x509.Certificate{
		SerialNumber:          serial,
		Subject:               pkix.Name{CommonName: commonName},
		NotBefore:             now.Add(-5 * time.Minute),
		NotAfter:              now.Add(10 * 365 * 24 * time.Hour),
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageKeyEncipherment,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth, x509.ExtKeyUsageClientAuth},
		BasicConstraintsValid: true,
		IsCA:                  false,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, publicKey, privateKey)
	if err != nil {
		return Identity{}, err
	}
	certificate, err := x509.ParseCertificate(der)
	if err != nil {
		return Identity{}, err
	}
	keyDER, err := x509.MarshalPKCS8PrivateKey(privateKey)
	if err != nil {
		return Identity{}, err
	}
	return Identity{
		PrivateKey:  privateKey,
		PublicKey:   publicKey,
		Certificate: certificate,
		CertPEM:     pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}),
		KeyPEM:      pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER}),
	}, nil
}

func (i Identity) Fingerprint() []byte {
	if i.Certificate == nil {
		return nil
	}
	sum := sha256.Sum256(i.Certificate.RawSubjectPublicKeyInfo)
	return sum[:]
}

func (i Identity) TLSCertificate() (tls.Certificate, error) {
	if len(i.CertPEM) == 0 || len(i.KeyPEM) == 0 {
		return tls.Certificate{}, errors.New("identity certificate is empty")
	}
	return tls.X509KeyPair(i.CertPEM, i.KeyPEM)
}
