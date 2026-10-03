package broadcast

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"sync"
)

type MessageID [16]byte

func NewMessageID() (MessageID, error) {
	var id MessageID
	_, err := rand.Read(id[:])
	if err == nil {
		id[6] = (id[6] & 0x0f) | 0x40
		id[8] = (id[8] & 0x3f) | 0x80
	}
	return id, err
}

func (id MessageID) String() string {
	buf := make([]byte, 32)
	hex.Encode(buf, id[:])
	return fmt.Sprintf("%s-%s-%s-%s-%s", buf[0:8], buf[8:12], buf[12:16], buf[16:20], buf[20:32])
}

func ParseMessageID(value string) (MessageID, error) {
	var id MessageID
	if len(value) != 36 || value[8] != '-' || value[13] != '-' || value[18] != '-' || value[23] != '-' {
		return id, errors.New("invalid message id")
	}
	compact := make([]byte, 0, 32)
	for i := range value {
		if value[i] == '-' {
			continue
		}
		compact = append(compact, value[i])
	}
	if _, err := hex.Decode(id[:], compact); err != nil {
		return MessageID{}, errors.New("invalid message id")
	}
	return id, nil
}

func (id MessageID) IsZero() bool { return id == (MessageID{}) }

type Message struct {
	ID                   MessageID    `bson:"message_id"`
	QueueSeq             uint64       `bson:"queue_seq"`
	Priority             uint16       `bson:"priority"`
	PrioritySet          bool         `bson:"priority_set"`
	CreatedAt            int64        `bson:"created_at"`
	ExpiresAt            int64        `bson:"expires_at"`
	DisplayText          string       `bson:"content"`
	DisplayPosition      *string      `bson:"display_position,omitempty"`
	DisplayDurationRatio *float64     `bson:"display_duration_ratio,omitempty"`
	Speech               []SpeechNode `bson:"speech,omitempty"`
}

type SpeechNode struct {
	Type       string       `bson:"type"`
	Value      string       `bson:"value,omitempty"`
	Count      uint32       `bson:"count,omitempty"`
	DurationMS uint32       `bson:"duration_ms,omitempty"`
	Children   []SpeechNode `bson:"children,omitempty"`
}

type Queue struct {
	mu    sync.Mutex
	items []*Message
	seq   uint64
}

func NewQueue() *Queue { return &Queue{} }

func (q *Queue) Enqueue(message Message) error {
	_, err := q.EnqueueWithSequence(message)
	return err
}

func (q *Queue) EnqueueWithSequence(message Message) (Message, error) {
	if message.ID == (MessageID{}) {
		return Message{}, errors.New("message ID is required")
	}
	q.mu.Lock()
	defer q.mu.Unlock()
	if message.QueueSeq == 0 {
		q.seq++
		message.QueueSeq = q.seq
	} else if message.QueueSeq > q.seq {
		q.seq = message.QueueSeq
	}
	copy := message
	q.items = append(q.items, &copy)
	return message, nil
}

func (q *Queue) Pop() *Message {
	q.mu.Lock()
	defer q.mu.Unlock()
	if len(q.items) == 0 {
		return nil
	}
	best := 0
	for i := 1; i < len(q.items); i++ {
		if less(q.items[i], q.items[best]) {
			best = i
		}
	}
	item := q.items[best]
	q.items = append(q.items[:best], q.items[best+1:]...)
	return item
}

// Requeue puts a previously popped message back without changing its ordering key.
func (q *Queue) Requeue(message Message) error {
	if message.ID.IsZero() {
		return errors.New("message ID is required")
	}
	q.mu.Lock()
	defer q.mu.Unlock()
	copy := message
	q.items = append(q.items, &copy)
	if message.QueueSeq > q.seq {
		q.seq = message.QueueSeq
	}
	return nil
}

func (q *Queue) Len() int {
	q.mu.Lock()
	defer q.mu.Unlock()
	return len(q.items)
}

func less(a, b *Message) bool {
	if a.Priority != b.Priority {
		return a.Priority < b.Priority
	}
	return a.QueueSeq < b.QueueSeq
}
