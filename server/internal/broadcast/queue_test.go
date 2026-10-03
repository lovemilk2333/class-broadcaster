package broadcast

import "testing"

func TestQueuePriorityAndFIFO(t *testing.T) {
	q := NewQueue()
	first, _ := NewMessageID()
	second, _ := NewMessageID()
	urgent, _ := NewMessageID()
	if err := q.Enqueue(Message{ID: first, Priority: 1024}); err != nil {
		t.Fatal(err)
	}
	if err := q.Enqueue(Message{ID: second, Priority: 1024}); err != nil {
		t.Fatal(err)
	}
	if err := q.Enqueue(Message{ID: urgent, Priority: 1}); err != nil {
		t.Fatal(err)
	}
	if got := q.Pop().ID; got != urgent {
		t.Fatalf("first message = %s, want urgent %s", got, urgent)
	}
	if got := q.Pop().ID; got != first {
		t.Fatalf("second message = %s, want first %s", got, first)
	}
	if got := q.Pop().ID; got != second {
		t.Fatalf("third message = %s, want second %s", got, second)
	}
	if q.Pop() != nil {
		t.Fatal("empty queue returned a message")
	}
}
