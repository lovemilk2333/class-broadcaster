package config

import (
	"crypto/rand"
	"encoding/binary"
	"errors"
	"strconv"
	"strings"
	"time"
)

const (
	ConfigIDRandomBytes                 = 4
	DefaultConfigMaxAge                 = 7 * 24 * time.Hour
	DefaultDisplayPosition              = "center"
	DefaultDisplayDurationRatio float64 = 0.2
)

var ErrInvalidConfigID = errors.New("invalid config id")

// ConfigID is an issued-at Unix millisecond timestamp and a random uint32.
func NewConfigID(now time.Time) (string, error) {
	var randomBytes [ConfigIDRandomBytes]byte
	if _, err := rand.Read(randomBytes[:]); err != nil {
		return "", err
	}
	randomValue := binary.BigEndian.Uint32(randomBytes[:])
	return strconv.FormatInt(now.UnixMilli(), 10) + "." + strconv.FormatUint(uint64(randomValue), 10), nil
}

func ConfigIDIssuedAt(id string) (time.Time, error) {
	timestamp, randomPart, ok := strings.Cut(id, ".")
	if !ok || timestamp == "" || randomPart == "" || strings.Contains(randomPart, ".") {
		return time.Time{}, ErrInvalidConfigID
	}
	ms, err := strconv.ParseInt(timestamp, 10, 64)
	if err != nil || ms <= 0 {
		return time.Time{}, ErrInvalidConfigID
	}
	randomValue, err := strconv.ParseUint(randomPart, 10, 32)
	if err != nil {
		return time.Time{}, ErrInvalidConfigID
	}
	_ = randomValue
	return time.UnixMilli(ms), nil
}

func NeedsRefresh(id string, now time.Time, maxAge time.Duration) bool {
	if id == "" {
		return true
	}
	issuedAt, err := ConfigIDIssuedAt(id)
	if err != nil || issuedAt.After(now) {
		return true
	}
	return now.Sub(issuedAt) > maxAge
}

type Snapshot struct {
	ID                   string        `bson:"config_id"`
	IssuedAt             int64         `bson:"issued_at"`
	HeartbeatInterval    time.Duration `bson:"heartbeat_interval"`
	HeartbeatTimeout     time.Duration `bson:"heartbeat_timeout"`
	MessageTTL           time.Duration `bson:"message_ttl"`
	MaxSpeechDepth       int32         `bson:"max_speech_depth"`
	MaxRepeatExpansion   int32         `bson:"max_repeat_expansion"`
	DisplayPosition      string        `bson:"default_display_position"`
	DisplayDurationRatio float64       `bson:"default_display_duration_ratio"`
}

func NewSnapshot(now time.Time) (Snapshot, error) {
	id, err := NewConfigID(now)
	if err != nil {
		return Snapshot{}, err
	}
	return Snapshot{
		ID:                   id,
		IssuedAt:             now.UnixMilli(),
		HeartbeatInterval:    15 * time.Second,
		HeartbeatTimeout:     45 * time.Second,
		MessageTTL:           24 * time.Hour,
		MaxSpeechDepth:       8,
		MaxRepeatExpansion:   100,
		DisplayPosition:      DefaultDisplayPosition,
		DisplayDurationRatio: DefaultDisplayDurationRatio,
	}, nil
}
