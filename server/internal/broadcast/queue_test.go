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

func TestValidateSpeechLimitsExpansionAndDepth(t *testing.T) {
	if err := ValidateSpeech([]SpeechNode{{Type: "repeat", Count: 2, Children: []SpeechNode{{Type: "text", Value: "hi"}}}}, 2, 4); err != nil {
		t.Fatalf("valid speech rejected: %v", err)
	}
	if err := ValidateSpeech([]SpeechNode{{Type: "repeat", Count: 3, Children: []SpeechNode{{Type: "text"}}}}, 2, 2); err == nil {
		t.Fatal("expansion limit was not enforced")
	}
	if err := ValidateSpeech([]SpeechNode{{Type: "repeat", Count: 1, Children: []SpeechNode{{Type: "repeat", Count: 1, Children: []SpeechNode{{Type: "text"}}}}}}, 1, 4); err == nil {
		t.Fatal("depth limit was not enforced")
	}
}
