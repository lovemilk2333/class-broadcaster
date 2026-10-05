package main

import (
	"archive/tar"
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/klauspost/compress/zstd"
	_ "modernc.org/sqlite"

	"lovemilk-class-broadcaster/server/internal/broadcast"
	"lovemilk-class-broadcaster/server/internal/config"
	"lovemilk-class-broadcaster/server/internal/identity"
	"lovemilk-class-broadcaster/server/internal/protocol"
	"lovemilk-class-broadcaster/server/internal/session"
)

// buildTarZstPackage builds a canonical files-v1 .tar.zst update package for tests.
func buildTarZstPackage(t *testing.T, metadata updatePackageMetadata, files map[string][]byte) []byte {
	t.Helper()
	var tarBuf bytes.Buffer
	tw := tar.NewWriter(&tarBuf)
	metadataBytes, err := json.Marshal(metadata)
	if err != nil {
		t.Fatal(err)
	}
	metadataBytes = append(metadataBytes, '\n')
	if err := tw.WriteHeader(&tar.Header{Name: "metadata.json", Mode: 0o600, Size: int64(len(metadataBytes)), Typeflag: tar.TypeReg}); err != nil {
		t.Fatal(err)
	}
	if _, err := tw.Write(metadataBytes); err != nil {
		t.Fatal(err)
	}
	for name, body := range files {
		if err := tw.WriteHeader(&tar.Header{Name: name, Mode: 0o600, Size: int64(len(body)), Typeflag: tar.TypeReg}); err != nil {
			t.Fatal(err)
		}
		if _, err := tw.Write(body); err != nil {
			t.Fatal(err)
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	encoder, err := zstd.NewWriter(&out, zstd.WithEncoderLevel(zstd.EncoderLevelFromZstd(3)))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := encoder.Write(tarBuf.Bytes()); err != nil {
		t.Fatal(err)
	}
	if err := encoder.Close(); err != nil {
		t.Fatal(err)
	}
	return out.Bytes()
}

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

func TestListenerProbeChallengeVerifiesBothEd25519Keys(t *testing.T) {
	serverPublic, serverPrivate, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	clientPublic, clientPrivate, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	nonce := make([]byte, 32)
	if _, err := rand.Read(nonce); err != nil {
		t.Fatal(err)
	}
	challenge := listenerProbeSigningBytes(nonce, serverPublic)
	if !ed25519.Verify(serverPublic, challenge, ed25519.Sign(serverPrivate, challenge)) {
		t.Fatal("server challenge signature did not verify")
	}
	if !ed25519.Verify(clientPublic, challenge, ed25519.Sign(clientPrivate, challenge)) {
		t.Fatal("client challenge signature did not verify")
	}
	changed := append([]byte(nil), challenge...)
	changed[len(changed)-1] ^= 1
	if ed25519.Verify(clientPublic, changed, ed25519.Sign(clientPrivate, challenge)) {
		t.Fatal("signature accepted a changed challenge")
	}
	if _, err := session.ClientID(clientPublic); err != nil {
		t.Fatalf("client identity fingerprint failed: %v", err)
	}
}

func TestLogBufferKeepsBoundedRecentLines(t *testing.T) {
	buffer := newLogBuffer(2)
	buffer.append([]byte("{\"msg\":\"one\"}\n{\"msg\":\"two\"}\n{\"msg\":\"three\"}\n"))
	lines := buffer.snapshot()
	if len(lines) != 2 || lines[0] != `{"msg":"two"}` || lines[1] != `{"msg":"three"}` {
		t.Fatalf("buffer lines = %#v", lines)
	}
	content, total, err := readLogPageFromLines(lines, 1, 10, logPageFilter{})
	if err != nil || total != 2 || !bytes.HasPrefix([]byte(content), []byte(`{"msg":"three"}`)) {
		t.Fatalf("memory log page = %q total=%d err=%v", content, total, err)
	}
}

func TestClientLogsEndpointReturnsOnlyUploadedClientLogs(t *testing.T) {
	clientLogs := newClientLogBuffer(2)
	clientID := strings.Repeat("a", 64)
	clientLogs.append(clientID, `{"time":"2026-10-04T10:00:00+08:00","level":"ERROR","msg":"client connection failed"}`)
	serverLogs := newLogBuffer(10)
	serverLogs.append([]byte(`{"time":"2026-10-04T10:00:01+08:00","level":"INFO","msg":"server handled client","client_id":"` + clientID + `"}`))
	manager, err := config.NewManager(time.Now())
	if err != nil {
		t.Fatal(err)
	}
	router := newRouterWithDependencies(broadcast.NewStore(), session.NewRegistry(), session.NewHub(), manager, nil, nil, serverLogs, clientLogs, identity.Identity{})
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/api/v1/devices/"+clientID+"/logs", nil))
	if recorder.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	if !bytes.Contains(recorder.Body.Bytes(), []byte("client connection failed")) || bytes.Contains(recorder.Body.Bytes(), []byte("server handled client")) {
		t.Fatalf("endpoint did not isolate uploaded client logs: %s", recorder.Body.String())
	}
}

func TestClientLogBufferCoalescesRefreshEvents(t *testing.T) {
	buffer := newClientLogBuffer(2)
	if !buffer.append("client", `{"msg":"first"}`) {
		t.Fatal("first client log should notify the frontend")
	}
	if buffer.append("client", `{"msg":"second"}`) {
		t.Fatal("client log updates should be coalesced")
	}
	if got := strings.Count(buffer.snapshot("client"), "\n") + 1; got != 2 {
		t.Fatalf("client log count=%d, want both uploaded lines", got)
	}
}

func TestClientLogPageSortsNewestTimestampFirst(t *testing.T) {
	content, total := readClientLogPage(strings.Join([]string{
		`{"time":"2026-10-04T10:00:00+08:00","msg":"older"}`,
		`{"time":"2026-10-04T10:00:02+08:00","msg":"newest"}`,
		`{"time":"2026-10-04T10:00:01+08:00","msg":"middle"}`,
	}, "\n"), 1, 2)
	if total != 3 || !strings.Contains(content, `"msg":"newest"`) || !strings.Contains(content, `"msg":"middle"`) || strings.Contains(content, `"msg":"older"`) {
		t.Fatalf("newest-first page content=%q total=%d", content, total)
	}
}

func TestHealthEndpoint(t *testing.T) {
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	newRouter().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d", recorder.Code)
	}
	var body healthResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body.Status != "ok" || body.Service != "class-broadcaster" {
		t.Fatalf("body = %#v", body)
	}
	if body.Version == "" || body.BuildDate == "" {
		t.Fatalf("version fields missing: %#v", body)
	}
	if body.TLSPort != 39002 || body.HTTPPort != 39003 || body.UDPPort != 39001 {
		t.Fatalf("ports = tls=%d http=%d udp=%d", body.TLSPort, body.HTTPPort, body.UDPPort)
	}
}

func TestEmbeddedFrontendSupportsGetAndHead(t *testing.T) {
	router := newRouter()
	for _, method := range []string{http.MethodGet, http.MethodHead} {
		recorder := httptest.NewRecorder()
		router.ServeHTTP(recorder, httptest.NewRequest(method, "/", nil))
		if recorder.Code != http.StatusOK {
			t.Fatalf("%s embedded frontend status = %d body=%s", method, recorder.Code, recorder.Body.String())
		}
	}
}

// TestConfigSnapshotPersistsListenerPolicy 验证服务端重启后从 SQLite 恢复监听参数。
func TestConfigSnapshotPersistsListenerPolicy(t *testing.T) {
	database := filepath.Join(t.TempDir(), "server.db")
	preferences, err := openServerPreferences(database)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Unix(1_700_000_000, 0)
	snapshot, err := config.NewSnapshot(now)
	if err != nil {
		t.Fatal(err)
	}
	snapshot.ListenerProbeInterval = 17 * time.Second
	snapshot.ListenerProbeDuration = 2 * time.Hour
	snapshot.ListenerProbeReset = 3 * 24 * time.Hour
	snapshot.ListenerLossThreshold = 35
	snapshot.ListenerIdleTimeout = 9 * time.Second
	if err := preferences.saveConfigSnapshot(snapshot); err != nil {
		t.Fatal(err)
	}
	if err := preferences.db.Close(); err != nil {
		t.Fatal(err)
	}

	reopened, err := openServerPreferences(database)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.db.Close()
	restored, err := reopened.loadConfigSnapshot(now.Add(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if restored.ID != snapshot.ID || restored.ListenerProbeInterval != snapshot.ListenerProbeInterval ||
		restored.ListenerProbeDuration != snapshot.ListenerProbeDuration || restored.ListenerProbeReset != snapshot.ListenerProbeReset ||
		restored.ListenerLossThreshold != snapshot.ListenerLossThreshold || restored.ListenerIdleTimeout != snapshot.ListenerIdleTimeout {
		t.Fatalf("listener policy was not persisted: got %#v want %#v", restored, snapshot)
	}
}

func TestPublishUpdateSendsSignedMetadataToSelectedOnlineClient(t *testing.T) {
	publicKey, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	registry := session.NewRegistry()
	device, err := registry.Approve(publicKey, "screen", time.Now())
	if err != nil {
		t.Fatal(err)
	}
	hub := session.NewHub()
	serverConn, clientConn := net.Pipe()
	defer clientConn.Close()
	defer serverConn.Close()
	unregister := hub.Register(device.ClientID, serverConn)
	defer unregister()
	manager, err := config.NewManager(time.Now())
	if err != nil {
		t.Fatal(err)
	}
	router := newRouterWithConfig(broadcast.NewStore(), registry, hub, manager)
	requestBody, err := json.Marshal(publishUpdateRequest{
		Component: "client", Version: "0.1.1", Platform: "windows-amd64", PayloadFormat: "files-v1",
		SHA256: strings.Repeat("a", 64), ClientVersion: "0.1.1",
		ReleaseURL: "https://updates.example/client.zst", Targets: []string{device.ClientID},
	})
	if err != nil {
		t.Fatal(err)
	}
	readResult := make(chan protocol.Packet, 1)
	readError := make(chan error, 1)
	go func() {
		packet, readErr := protocol.ReadPacket(clientConn)
		if readErr != nil {
			readError <- readErr
			return
		}
		readResult <- packet
	}()
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/v1/updates/publish", bytes.NewReader(requestBody))
	request.Header.Set("Content-Type", "application/json")
	router.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK || !bytes.Contains(recorder.Body.Bytes(), []byte(`"sent":[`)) {
		t.Fatalf("publish status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	select {
	case err := <-readError:
		t.Fatal(err)
	case packet := <-readResult:
		if packet.Type != protocol.UpdateAvailable {
			t.Fatalf("packet type = %#x, want UpdateAvailable", packet.Type)
		}
		var payload updateAvailablePayload
		if err := protocol.UnmarshalBSON(packet.Payload, &payload); err != nil {
			t.Fatal(err)
		}
		if payload.Component != "client" || payload.Version != "0.1.1" || payload.ReleaseURL != "https://updates.example/client.zst" {
			t.Fatalf("update payload = %#v", payload)
		}
	}
}

func TestPublishUpdateAllowsLocalPackageMetadataWithoutSignature(t *testing.T) {
	input := publishUpdateRequest{
		Component: "client", Version: "0.1.1", Platform: "windows-amd64", PayloadFormat: "files-v1",
		SHA256: strings.Repeat("a", 64), Targets: []string{strings.Repeat("a", 64)}, ClientVersion: "0.1.1",
	}
	if err := validatePublishUpdate(&input); err != nil {
		t.Fatalf("local update metadata was rejected: %v", err)
	}
	if input.Component != "client" || len(input.Components) != 1 || input.Components[0] != "client" {
		t.Fatalf("normalized components=%#v primary=%q", input.Components, input.Component)
	}
}

func TestNormalizeUpdateComponentsBundleOrder(t *testing.T) {
	components, primary, err := normalizeUpdateComponents([]string{"client", "updater"}, "")
	if err != nil {
		t.Fatal(err)
	}
	if primary != "bundle" {
		t.Fatalf("primary=%q want bundle", primary)
	}
	if len(components) != 2 || components[0] != "updater" || components[1] != "client" {
		t.Fatalf("components=%#v want [updater client]", components)
	}
	legacy, primary, err := normalizeUpdateComponents(nil, "bundle")
	if err != nil {
		t.Fatal(err)
	}
	if primary != "bundle" || len(legacy) != 2 || legacy[0] != "updater" {
		t.Fatalf("legacy bundle=%#v primary=%q", legacy, primary)
	}
}

func TestFilesV1ManifestMatchesPythonSortKeysOrder(t *testing.T) {
	// path/sha256/size key order must match Python sort_keys=True.
	entries := []updateManifestEntry{
		{Path: "b/file.bin", SHA256: strings.Repeat("b", 64), Size: 2},
		{Path: "a-file.bin", SHA256: strings.Repeat("a", 64), Size: 1},
	}
	manifest, err := marshalFilesV1Manifest(entries)
	if err != nil {
		t.Fatal(err)
	}
	// a-file before b/file (string order), keys alphabetically path,sha256,size.
	want := `[{"path":"a-file.bin","sha256":"` + strings.Repeat("a", 64) + `","size":1},{"path":"b/file.bin","sha256":"` + strings.Repeat("b", 64) + `","size":2}]`
	if string(manifest) != want {
		t.Fatalf("manifest=%s want=%s", manifest, want)
	}
}

func TestUpdateDownloadTokenRoundTrip(t *testing.T) {
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Unix(1_700_000_000, 0)
	token, err := signUpdateDownloadToken(privateKey, updateDownloadClaims{
		SHA256: strings.Repeat("ab", 32), ClientID: strings.Repeat("cd", 32), ExpiresAt: now.Add(time.Hour).Unix(), Nonce: "n1",
	})
	if err != nil {
		t.Fatal(err)
	}
	claims, err := verifyUpdateDownloadToken(publicKey, token, now)
	if err != nil {
		t.Fatal(err)
	}
	if claims.SHA256 != strings.Repeat("ab", 32) || claims.ClientID != strings.Repeat("cd", 32) {
		t.Fatalf("claims=%#v", claims)
	}
	if _, err := verifyUpdateDownloadToken(publicKey, token, now.Add(2*time.Hour)); err == nil {
		t.Fatal("expired token accepted")
	}
}

func TestParseUpdatePackageDerivesMetadataFromUploadedArchive(t *testing.T) {
	content := []byte("client payload")
	entryHash := sha256.Sum256(content)
	manifest, err := marshalFilesV1Manifest([]updateManifestEntry{{Path: "client.exe", SHA256: hex.EncodeToString(entryHash[:]), Size: len(content)}})
	if err != nil {
		t.Fatal(err)
	}
	manifestHash := sha256.Sum256(manifest)
	metadata := updatePackageMetadata{
		Component: "client", Components: []string{"client"}, Version: "1.2.3", Platform: "windows-amd64", PayloadFormat: "files-v1",
		SHA256: hex.EncodeToString(manifestHash[:]),
	}
	packageBytes := buildTarZstPackage(t, metadata, map[string][]byte{"client.exe": content})
	input, normalized, err := parseUpdatePackage(packageBytes)
	if err != nil {
		t.Fatal(err)
	}
	if len(normalized) < 4 || !bytes.Equal(normalized[:4], []byte{0x28, 0xb5, 0x2f, 0xfd}) {
		header := normalized
		if len(header) > 4 {
			header = header[:4]
		}
		t.Fatalf("normalized package is not zstd: %x", header)
	}
	if input.Component != "client" || input.Version != "1.2.3" || input.ClientVersion != "1.2.3" || input.SHA256 != metadata.SHA256 {
		t.Fatalf("derived metadata = %#v", input)
	}
	if len(input.Components) != 1 || input.Components[0] != "client" {
		t.Fatalf("components=%#v", input.Components)
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

func TestCreateBroadcastSnapshotsApprovedClientsAndConfigTTL(t *testing.T) {
	publicKey, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Unix(1_700_000_000, 0)
	registry := session.NewRegistry()
	device, err := registry.Approve(publicKey, "screen", now)
	if err != nil {
		t.Fatal(err)
	}
	manager, err := config.NewManager(now)
	if err != nil {
		t.Fatal(err)
	}
	ttl := 48 * time.Hour
	if _, err := manager.Update(config.Update{MessageTTL: &ttl}, now.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	router := newRouterWithConfig(broadcast.NewStore(), registry, session.NewHub(), manager)
	request := httptest.NewRequest(http.MethodPost, "/api/v1/messages", bytes.NewBufferString(`{"content":"offline delivery"}`))
	request.Header.Set("Content-Type", "application/json")
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusCreated {
		t.Fatalf("status = %d body=%s", recorder.Code, recorder.Body.String())
	}
	var created struct {
		TargetClientIDs []string `json:"target_client_ids"`
		ExpiresAt       int64    `json:"expires_at"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &created); err != nil {
		t.Fatal(err)
	}
	if len(created.TargetClientIDs) != 1 || created.TargetClientIDs[0] != device.ClientID {
		t.Fatalf("broadcast targets = %#v, want %s", created.TargetClientIDs, device.ClientID)
	}
	if expiry := time.UnixMilli(created.ExpiresAt); expiry.Before(time.Now().Add(47 * time.Hour)) {
		t.Fatalf("message TTL was not taken from config: expires_at=%s", expiry)
	}
}

func TestWithdrawMessage(t *testing.T) {
	publicKey, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	registry := session.NewRegistry()
	if _, err := registry.Approve(publicKey, "screen", time.Now()); err != nil {
		t.Fatal(err)
	}
	manager, err := config.NewManager(time.Now())
	if err != nil {
		t.Fatal(err)
	}
	router := newRouterWithConfig(broadcast.NewStore(), registry, session.NewHub(), manager)
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
	if before.Code != http.StatusOK || !bytes.Contains(before.Body.Bytes(), []byte(`"heartbeat_interval_seconds":15`)) || !bytes.Contains(before.Body.Bytes(), []byte(`"ack_timeout_seconds":300`)) {
		t.Fatalf("config status = %d body=%s", before.Code, before.Body.String())
	}
	request := httptest.NewRequest(http.MethodPost, "/api/v1/config", bytes.NewBufferString(`{"heartbeat_interval_seconds":20,"heartbeat_timeout_seconds":60,"message_ttl_hours":48,"ack_timeout_seconds":90,"immediate":true}`))
	request.Header.Set("Content-Type", "application/json")
	after := httptest.NewRecorder()
	router.ServeHTTP(after, request)
	if after.Code != http.StatusOK || !bytes.Contains(after.Body.Bytes(), []byte(`"heartbeat_interval_seconds":20`)) || !bytes.Contains(after.Body.Bytes(), []byte(`"ack_timeout_seconds":90`)) {
		t.Fatalf("update status = %d body=%s", after.Code, after.Body.String())
	}
	invalid := httptest.NewRecorder()
	request = httptest.NewRequest(http.MethodPost, "/api/v1/config", bytes.NewBufferString(`{"ack_timeout_seconds":604801}`))
	request.Header.Set("Content-Type", "application/json")
	router.ServeHTTP(invalid, request)
	if invalid.Code != http.StatusBadRequest {
		t.Fatalf("out-of-range acknowledgement timeout status = %d body=%s", invalid.Code, invalid.Body.String())
	}
}

func TestMaintenancePurgeExpiredEndpoint(t *testing.T) {
	store := broadcast.NewStore()
	now := time.Unix(1_700_000_000, 0)
	staleCreated := now.Add(-186 * 24 * time.Hour)
	staleID, err := broadcast.NewMessageID()
	if err != nil {
		t.Fatal(err)
	}
	freshID, err := broadcast.NewMessageID()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Create(broadcast.Message{
		ID: staleID, DisplayText: "stale message", PrioritySet: true, Priority: 10,
		CreatedAt: staleCreated.UnixMilli(), ExpiresAt: staleCreated.Add(24 * time.Hour).UnixMilli(),
	}, nil, staleCreated); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Create(broadcast.Message{
		ID: freshID, DisplayText: "fresh message", PrioritySet: true, Priority: 10,
	}, nil, now); err != nil {
		t.Fatal(err)
	}

	registry := session.NewRegistry()
	staleKey, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	staleDevice, err := registry.Approve(staleKey, "stale-device", staleCreated)
	if err != nil {
		t.Fatal(err)
	}
	registry.MarkSeen(staleKey, staleCreated)

	freshKey, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := registry.Approve(freshKey, "fresh-device", now); err != nil {
		t.Fatal(err)
	}

	manager, err := config.NewManager(now)
	if err != nil {
		t.Fatal(err)
	}
	// freeze "now" for the endpoint by invoking the helper directly first, then assert HTTP.
	// The HTTP handler uses time.Now(); for deterministic counts call purgeExpiredHistory.
	direct := purgeExpiredHistory(store, registry, now)
	if direct.MessagesRemoved != 1 {
		t.Fatalf("direct purge messages=%d want 1", direct.MessagesRemoved)
	}
	if direct.DevicesRemoved != 1 {
		t.Fatalf("direct purge devices=%d want 1", direct.DevicesRemoved)
	}
	if _, err := registry.Get(staleDevice.ClientID); err == nil {
		t.Fatal("stale device still present after purge")
	}

	router := newRouterWithConfig(broadcast.NewStore(), session.NewRegistry(), session.NewHub(), manager)
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, httptest.NewRequest(http.MethodPost, "/api/v1/maintenance/purge-expired", nil))
	if recorder.Code != http.StatusOK {
		t.Fatalf("purge status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	var result retentionCleanupResult
	if err := json.Unmarshal(recorder.Body.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	if result.MaxAgeDays != 185 || result.RanAt <= 0 {
		t.Fatalf("unexpected response %#v", result)
	}
	if !bytes.Contains(recorder.Body.Bytes(), []byte(`"messages_removed"`)) || !bytes.Contains(recorder.Body.Bytes(), []byte(`"devices_removed"`)) {
		t.Fatalf("response missing counters body=%s", recorder.Body.String())
	}
	_ = freshID
}

func TestParseUpdatePackageAcceptsNestedPathOrdering(t *testing.T) {
	// Paths where string order differs from pathlib.Path component order:
	// string: a-b/f.bin < a/x/f.bin ; Path: a/x/f.bin < a-b/f.bin
	contentA := []byte("1")
	contentB := []byte("2")
	hashA := sha256.Sum256(contentA)
	hashB := sha256.Sum256(contentB)
	manifest, err := marshalFilesV1Manifest([]updateManifestEntry{
		{Path: "a-b/f.bin", SHA256: hex.EncodeToString(hashA[:]), Size: len(contentA)},
		{Path: "a/x/f.bin", SHA256: hex.EncodeToString(hashB[:]), Size: len(contentB)},
	})
	if err != nil {
		t.Fatal(err)
	}
	manifestHash := sha256.Sum256(manifest)
	metadata := updatePackageMetadata{
		Component: "bundle", Components: []string{"updater", "client"}, Version: "0.0.2", Platform: "windows-amd64", PayloadFormat: "files-v1",
		SHA256: hex.EncodeToString(manifestHash[:]), ClientVersion: "0.0.2", UpdaterVersion: "0.0.2",
	}
	packageBytes := buildTarZstPackage(t, metadata, map[string][]byte{"a-b/f.bin": contentA, "a/x/f.bin": contentB})
	input, normalized, err := parseUpdatePackage(packageBytes)
	if err != nil {
		t.Fatalf("nested-path package rejected: %v", err)
	}
	if !strings.EqualFold(input.SHA256, hex.EncodeToString(manifestHash[:])) {
		t.Fatalf("sha256=%s want=%s", input.SHA256, hex.EncodeToString(manifestHash[:]))
	}
	if len(normalized) < 4 {
		t.Fatal("normalized package empty")
	}
	if input.Component != "bundle" || len(input.Components) != 2 || input.Components[0] != "updater" || input.Components[1] != "client" {
		t.Fatalf("components=%#v primary=%q", input.Components, input.Component)
	}
}

func TestUpdateRecordsSurviveReopen(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "server.db")
	prefs, err := openServerPreferences(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	digest := strings.Repeat("ab", 32)
	record := updateRecord{
		UpdateID: "upd-1", Component: "client", Components: []string{"client"},
		Version: "1.2.3", Platform: "windows-amd64", ClientVersion: "1.2.3",
		SHA256: digest, Status: "published", CreatedAt: 1_700_000_000_000, Seq: 1,
	}
	metadata, _ := json.Marshal(publishUpdateRequest{
		Component: "client", Components: []string{"client"}, Version: "1.2.3",
		ClientVersion: "1.2.3", Platform: "windows-amd64", PayloadFormat: "files-v1", SHA256: digest,
	})
	if err := prefs.saveUpdateFile(record, metadata, ""); err != nil {
		t.Fatal(err)
	}
	items, err := prefs.updateRecords()
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 1 || items[0].UpdateID != "upd-1" || items[0].Status != "published" {
		t.Fatalf("first list %#v", items)
	}
	if err := prefs.db.Close(); err != nil {
		t.Fatal(err)
	}

	reopened, err := openServerPreferences(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.db.Close()
	again, err := reopened.updateRecords()
	if err != nil {
		t.Fatal(err)
	}
	if len(again) != 1 || again[0].UpdateID != "upd-1" || again[0].Status != "published" || again[0].SHA256 != digest {
		t.Fatalf("after reopen %#v", again)
	}
}

func TestUpdateRecordsListWorksWithoutStatusDetailColumn(t *testing.T) {
	// Simulate a pre-migration DB that never had status_detail, then open via openServerPreferences
	// which must migrate and still list rows.
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "server.db")
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`CREATE TABLE update_records (
		update_id TEXT PRIMARY KEY,
		component TEXT NOT NULL DEFAULT '',
		version TEXT NOT NULL DEFAULT '',
		platform TEXT NOT NULL DEFAULT '',
		client_version TEXT NOT NULL DEFAULT '',
		updater_version TEXT NOT NULL DEFAULT '',
		sha256 TEXT NOT NULL,
		package_path TEXT NOT NULL,
		metadata BLOB NOT NULL,
		status TEXT NOT NULL,
		created_at INTEGER NOT NULL,
		withdrawn_at INTEGER NOT NULL DEFAULT 0,
		seq INTEGER NOT NULL DEFAULT 0
	)`); err != nil {
		t.Fatal(err)
	}
	digest := strings.Repeat("cd", 32)
	if _, err := db.Exec(
		`INSERT INTO update_records(update_id, component, version, platform, client_version, updater_version, sha256, package_path, metadata, status, created_at, seq)
		 VALUES (?, 'client', '9.9.9', 'windows-amd64', '9.9.9', '', ?, '', '{}', 'published', 1, 1)`,
		"legacy-1", digest,
	); err != nil {
		t.Fatal(err)
	}
	_ = db.Close()

	prefs, err := openServerPreferences(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer prefs.db.Close()
	items, err := prefs.updateRecords()
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 1 || items[0].UpdateID != "legacy-1" || items[0].Status != "published" {
		t.Fatalf("list after migrate %#v", items)
	}
}
