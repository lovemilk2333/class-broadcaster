package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestEventBrokerCoalescesSlowSubscribersIntoFullRefresh(t *testing.T) {
	broker := newEventBroker()
	events, unsubscribe := broker.subscribe()
	defer unsubscribe()

	for i := 0; i < cap(events); i++ {
		broker.publish("messages")
	}
	broker.publish("devices")

	if got := len(events); got != 1 {
		t.Fatalf("queued events = %d, want one coalesced event", got)
	}
	if event := <-events; event != "all" {
		t.Fatalf("coalesced event = %q, want all", event)
	}
}

func TestHealthEndpoint(t *testing.T) {
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	newRouter().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d", recorder.Code)
	}
	if body := recorder.Body.String(); body != `{"status":"ok","service":"class-broadcaster"}` {
		t.Fatalf("body = %q", body)
	}
}

func TestCreateAndListMessage(t *testing.T) {
	router := newRouter()
	request := httptest.NewRequest(http.MethodPost, "/api/v1/messages", bytes.NewBufferString(`{"content":"请王小明","target_client_ids":["screen-1"]}`))
	request.Header.Set("Content-Type", "application/json")
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusCreated {
		t.Fatalf("status = %d body=%s", recorder.Code, recorder.Body.String())
	}
	list := httptest.NewRecorder()
	router.ServeHTTP(list, httptest.NewRequest(http.MethodGet, "/api/v1/messages", nil))
	if list.Code != http.StatusOK || !bytes.Contains(list.Body.Bytes(), []byte(`"status":"pending"`)) {
		t.Fatalf("list status = %d body=%s", list.Code, list.Body.String())
	}
}

func TestWithdrawMessage(t *testing.T) {
	router := newRouter()
	request := httptest.NewRequest(http.MethodPost, "/api/v1/messages", bytes.NewBufferString(`{"content":"请撤回"}`))
	request.Header.Set("Content-Type", "application/json")
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusCreated {
		t.Fatalf("create status = %d body=%s", recorder.Code, recorder.Body.String())
	}
	var created struct {
		MessageID string `json:"message_id"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &created); err != nil {
		t.Fatal(err)
	}
	withdraw := httptest.NewRecorder()
	router.ServeHTTP(withdraw, httptest.NewRequest(http.MethodPost, "/api/v1/messages/"+created.MessageID+"/withdraw", nil))
	if withdraw.Code != http.StatusOK || !bytes.Contains(withdraw.Body.Bytes(), []byte(`"status":"withdrawn"`)) {
		t.Fatalf("withdraw status = %d body=%s", withdraw.Code, withdraw.Body.String())
	}
}

func TestApproveDevice(t *testing.T) {
	router := newRouter()
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/v1/devices", bytes.NewBufferString(`{"client_id":"000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f","label":"screen-1"}`))
	request.Header.Set("Content-Type", "application/json")
	router.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusCreated {
		t.Fatalf("status = %d body=%s", recorder.Code, recorder.Body.String())
	}
	list := httptest.NewRecorder()
	router.ServeHTTP(list, httptest.NewRequest(http.MethodGet, "/api/v1/devices", nil))
	if list.Code != http.StatusOK || !bytes.Contains(list.Body.Bytes(), []byte("screen-1")) {
		t.Fatalf("list status = %d body=%s", list.Code, list.Body.String())
	}
}

func TestConfigEndpointUpdatesSnapshot(t *testing.T) {
	router := newRouter()
	before := httptest.NewRecorder()
	router.ServeHTTP(before, httptest.NewRequest(http.MethodGet, "/api/v1/config", nil))
	if before.Code != http.StatusOK || !bytes.Contains(before.Body.Bytes(), []byte(`"heartbeat_interval_seconds":15`)) {
		t.Fatalf("config status = %d body=%s", before.Code, before.Body.String())
	}
	request := httptest.NewRequest(http.MethodPost, "/api/v1/config", bytes.NewBufferString(`{"heartbeat_interval_seconds":20,"heartbeat_timeout_seconds":60,"message_ttl_hours":48,"immediate":true}`))
	request.Header.Set("Content-Type", "application/json")
	after := httptest.NewRecorder()
	router.ServeHTTP(after, request)
	if after.Code != http.StatusOK || !bytes.Contains(after.Body.Bytes(), []byte(`"heartbeat_interval_seconds":20`)) {
		t.Fatalf("update status = %d body=%s", after.Code, after.Body.String())
	}
}
