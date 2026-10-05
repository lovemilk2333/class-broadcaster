package config

import (
	"errors"
	"sync"
	"time"
)

type Manager struct {
	mu          sync.RWMutex
	snapshot    Snapshot
	subscribers map[chan Snapshot]struct{}
	persist     func(Snapshot) error
}

func NewManager(now time.Time) (*Manager, error) {
	snapshot, err := NewSnapshot(now)
	if err != nil {
		return nil, err
	}
	return NewManagerFromSnapshot(snapshot)
}

// NewManagerFromSnapshot 使用已经持久化的快照恢复配置管理器。
func NewManagerFromSnapshot(snapshot Snapshot) (*Manager, error) {
	if snapshot.ID == "" || snapshot.IssuedAt <= 0 {
		return nil, errors.New("config snapshot is incomplete")
	}
	if snapshot.AckTimeout <= 0 {
		snapshot.AckTimeout = DefaultAckTimeout
	}
	if snapshot.ListenerProbeInterval <= 0 {
		snapshot.ListenerProbeInterval = DefaultListenerProbeInterval
	}
	if snapshot.ListenerProbeDuration <= 0 {
		snapshot.ListenerProbeDuration = DefaultListenerProbeDuration
	}
	if snapshot.ListenerProbeReset <= 0 {
		snapshot.ListenerProbeReset = DefaultListenerProbeReset
	}
	if snapshot.ListenerLossThreshold < 0 || snapshot.ListenerLossThreshold > 100 {
		snapshot.ListenerLossThreshold = DefaultListenerLossThreshold
	}
	if snapshot.ListenerIdleTimeout <= 0 {
		snapshot.ListenerIdleTimeout = DefaultListenerIdleTimeout
	}
	return &Manager{snapshot: cloneSnapshot(snapshot), subscribers: make(map[chan Snapshot]struct{})}, nil
}

// SetPersistence 设置配置发布后的持久化回调，回调失败时更新不会生效。
func (m *Manager) SetPersistence(persist func(Snapshot) error) {
	m.mu.Lock()
	m.persist = persist
	m.mu.Unlock()
}

func (m *Manager) Current() Snapshot {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return cloneSnapshot(m.snapshot)
}

type Update struct {
	HeartbeatInterval     *time.Duration
	HeartbeatTimeout      *time.Duration
	MessageTTL            *time.Duration
	AckTimeout            *time.Duration
	MaxSpeechDepth        *int32
	MaxRepeatExpansion    *int32
	DisplayPosition       *string
	DisplayDurationRatio  *float64
	ListenerProbeInterval *time.Duration
	ListenerProbeDuration *time.Duration
	ListenerProbeReset    *time.Duration
	ListenerLossThreshold *int32
	ListenerIdleTimeout   *time.Duration
}

func (m *Manager) Update(update Update, now time.Time) (Snapshot, error) {
	m.mu.Lock()
	next := cloneSnapshot(m.snapshot)
	if update.HeartbeatInterval != nil {
		if *update.HeartbeatInterval <= 0 {
			m.mu.Unlock()
			return Snapshot{}, errors.New("heartbeat interval must be positive")
		}
		next.HeartbeatInterval = *update.HeartbeatInterval
	}
	if update.HeartbeatTimeout != nil {
		if *update.HeartbeatTimeout <= 0 || *update.HeartbeatTimeout < next.HeartbeatInterval {
			m.mu.Unlock()
			return Snapshot{}, errors.New("heartbeat timeout must be at least the interval")
		}
		next.HeartbeatTimeout = *update.HeartbeatTimeout
	}
	if update.MessageTTL != nil {
		if *update.MessageTTL <= 0 {
			m.mu.Unlock()
			return Snapshot{}, errors.New("message TTL must be positive")
		}
		next.MessageTTL = *update.MessageTTL
	}
	if update.AckTimeout != nil {
		if *update.AckTimeout <= 0 || *update.AckTimeout > MaxAckTimeout {
			m.mu.Unlock()
			return Snapshot{}, errors.New("ack timeout must be between 1 second and 7 days")
		}
		next.AckTimeout = *update.AckTimeout
	}
	if update.MaxSpeechDepth != nil {
		if *update.MaxSpeechDepth < 1 || *update.MaxSpeechDepth > 64 {
			m.mu.Unlock()
			return Snapshot{}, errors.New("speech depth must be between 1 and 64")
		}
		next.MaxSpeechDepth = *update.MaxSpeechDepth
	}
	if update.MaxRepeatExpansion != nil {
		if *update.MaxRepeatExpansion < 1 || *update.MaxRepeatExpansion > 10000 {
			m.mu.Unlock()
			return Snapshot{}, errors.New("repeat expansion must be between 1 and 10000")
		}
		next.MaxRepeatExpansion = *update.MaxRepeatExpansion
	}
	if update.DisplayPosition != nil {
		if !ValidDisplayPosition(*update.DisplayPosition) {
			m.mu.Unlock()
			return Snapshot{}, errors.New("display position is invalid")
		}
		next.DisplayPosition = *update.DisplayPosition
	}
	if update.DisplayDurationRatio != nil {
		if *update.DisplayDurationRatio < 0 || *update.DisplayDurationRatio > 120 {
			m.mu.Unlock()
			return Snapshot{}, errors.New("display duration ratio must be between 0 and 120")
		}
		next.DisplayDurationRatio = *update.DisplayDurationRatio
	}
	if update.ListenerProbeInterval != nil {
		if *update.ListenerProbeInterval < time.Second || *update.ListenerProbeInterval > time.Hour {
			m.mu.Unlock()
			return Snapshot{}, errors.New("listener probe interval must be between 1 second and 1 hour")
		}
		next.ListenerProbeInterval = *update.ListenerProbeInterval
	}
	if update.ListenerProbeDuration != nil {
		if *update.ListenerProbeDuration < time.Hour || *update.ListenerProbeDuration > 7*24*time.Hour {
			m.mu.Unlock()
			return Snapshot{}, errors.New("listener probe duration must be between 1 hour and 7 days")
		}
		next.ListenerProbeDuration = *update.ListenerProbeDuration
	}
	if update.ListenerProbeReset != nil {
		if *update.ListenerProbeReset < 24*time.Hour || *update.ListenerProbeReset > 365*24*time.Hour {
			m.mu.Unlock()
			return Snapshot{}, errors.New("listener probe reset must be between 1 day and 365 days")
		}
		next.ListenerProbeReset = *update.ListenerProbeReset
	}
	if update.ListenerLossThreshold != nil {
		if *update.ListenerLossThreshold < 0 || *update.ListenerLossThreshold > 100 {
			m.mu.Unlock()
			return Snapshot{}, errors.New("listener loss threshold must be between 0 and 100")
		}
		next.ListenerLossThreshold = *update.ListenerLossThreshold
	}
	if update.ListenerIdleTimeout != nil {
		if *update.ListenerIdleTimeout < time.Second || *update.ListenerIdleTimeout > time.Hour {
			m.mu.Unlock()
			return Snapshot{}, errors.New("listener idle timeout must be between 1 second and 1 hour")
		}
		next.ListenerIdleTimeout = *update.ListenerIdleTimeout
	}
	id, err := NewConfigID(now)
	if err != nil {
		m.mu.Unlock()
		return Snapshot{}, err
	}
	next.ID = id
	next.IssuedAt = now.UnixMilli()
	if m.persist != nil {
		if err := m.persist(next); err != nil {
			m.mu.Unlock()
			return Snapshot{}, err
		}
	}
	m.snapshot = next
	updated := cloneSnapshot(next)
	m.mu.Unlock()
	return updated, nil
}

func ValidDisplayPosition(value string) bool {
	switch value {
	case "top-left", "top", "top-right", "left", "center", "right", "bottom-left", "bottom", "bottom-right":
		return true
	default:
		return false
	}
}

func (m *Manager) Subscribe() (<-chan Snapshot, func()) {
	channel := make(chan Snapshot, 1)
	m.mu.Lock()
	m.subscribers[channel] = struct{}{}
	m.mu.Unlock()
	return channel, func() {
		m.mu.Lock()
		delete(m.subscribers, channel)
		close(channel)
		m.mu.Unlock()
	}
}

func (m *Manager) Publish(snapshot Snapshot) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	for channel := range m.subscribers {
		select {
		case channel <- cloneSnapshot(snapshot):
		default:
			select {
			case <-channel:
			default:
			}
			select {
			case channel <- cloneSnapshot(snapshot):
			default:
			}
		}
	}
}

func cloneSnapshot(snapshot Snapshot) Snapshot {
	return snapshot
}
