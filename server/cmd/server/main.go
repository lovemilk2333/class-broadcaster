package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/tls"
	"database/sql"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"
	_ "modernc.org/sqlite"

	"lovemilk-class-broadcaster/server/internal/broadcast"
	"lovemilk-class-broadcaster/server/internal/config"
	"lovemilk-class-broadcaster/server/internal/discovery"
	"lovemilk-class-broadcaster/server/internal/identity"
	"lovemilk-class-broadcaster/server/internal/protocol"
	"lovemilk-class-broadcaster/server/internal/session"
)

type healthResponse struct {
	Status  string `json:"status"`
	Service string `json:"service"`
}

type serverPreferences struct {
	db *sql.DB
	mu sync.Mutex
}

func openServerPreferences(path string) (*serverPreferences, error) {
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	if _, err := db.Exec(`CREATE TABLE IF NOT EXISTS server_preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL)`); err != nil {
		_ = db.Close()
		return nil, err
	}
	// 配置快照与分页偏好共用服务端数据库，保证重启后协议配置不回退。
	if _, err := db.Exec(`CREATE TABLE IF NOT EXISTS config_snapshots (id INTEGER PRIMARY KEY CHECK (id = 1), payload BLOB NOT NULL, updated_at INTEGER NOT NULL)`); err != nil {
		_ = db.Close()
		return nil, err
	}
	return &serverPreferences{db: db}, nil
}

func (p *serverPreferences) pageSize() int {
	if p == nil || p.db == nil {
		return 25
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	var value int
	if err := p.db.QueryRow(`SELECT value FROM server_preferences WHERE key = 'page_size'`).Scan(&value); err != nil || !validPageSize(value) {
		return 25
	}
	return value
}

func (p *serverPreferences) setPageSize(value int) error {
	if !validPageSize(value) {
		return errors.New("page size must be one of 10, 15, 20, 25, 30, 50, 100 or 200")
	}
	if p == nil || p.db == nil {
		return nil
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	_, err := p.db.Exec(`INSERT INTO server_preferences(key, value) VALUES ('page_size', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value`, value)
	return err
}

// loadConfigSnapshot 读取持久化配置；首次启动时创建默认快照。
func (p *serverPreferences) loadConfigSnapshot(now time.Time) (config.Snapshot, error) {
	if p == nil || p.db == nil {
		return config.NewSnapshot(now)
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	var payload []byte
	err := p.db.QueryRow(`SELECT payload FROM config_snapshots WHERE id = 1`).Scan(&payload)
	if errors.Is(err, sql.ErrNoRows) {
		return config.NewSnapshot(now)
	}
	if err != nil {
		return config.Snapshot{}, err
	}
	var snapshot config.Snapshot
	if err := json.Unmarshal(payload, &snapshot); err != nil {
		return config.Snapshot{}, err
	}
	if snapshot.ID == "" || snapshot.IssuedAt <= 0 {
		return config.Snapshot{}, errors.New("persisted config snapshot is incomplete")
	}
	return snapshot, nil
}

// saveConfigSnapshot 将已发布配置原子写入 SQLite。
func (p *serverPreferences) saveConfigSnapshot(snapshot config.Snapshot) error {
	if p == nil || p.db == nil {
		return nil
	}
	payload, err := json.Marshal(snapshot)
	if err != nil {
		return err
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	_, err = p.db.Exec(`INSERT INTO config_snapshots(id, payload, updated_at) VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at`, payload, time.Now().UnixMilli())
	return err
}

func validPageSize(value int) bool {
	switch value {
	case 10, 15, 20, 25, 30, 50, 100, 200:
		return true
	default:
		return false
	}
}

type eventBroker struct {
	mu      sync.RWMutex
	clients map[chan string]struct{}
}

func newEventBroker() *eventBroker { return &eventBroker{clients: make(map[chan string]struct{})} }

func (b *eventBroker) subscribe() (chan string, func()) {
	channel := make(chan string, 8)
	b.mu.Lock()
	b.clients[channel] = struct{}{}
	b.mu.Unlock()
	return channel, func() {
		b.mu.Lock()
		if _, ok := b.clients[channel]; ok {
			delete(b.clients, channel)
			close(channel)
		}
		b.mu.Unlock()
	}
}

func (b *eventBroker) publish(event string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	for channel := range b.clients {
		select {
		case channel <- event:
		default:
			// A slow browser may not be able to consume events quickly enough.
			// Preserve correctness by replacing the backlog with one full-refresh
			// notification instead of silently losing an invalidation.
			for len(channel) > 0 {
				<-channel
			}
			channel <- "all"
		}
	}
}

var managementEvents = newEventBroker()

type logEventWriter struct {
	writer io.Writer
}

func (w logEventWriter) Write(p []byte) (int, error) {
	n, err := w.writer.Write(p)
	if n > 0 && !bytes.Contains(p, []byte(`"path":"/api/v1/logs"`)) && !bytes.Contains(p, []byte(`"path":"/api/v1/events"`)) {
		managementEvents.publish("logs")
	}
	return n, err
}

func main() {
	httpAddr := flag.String("http-addr", "127.0.0.1:39003", "management API listen address")
	udpAddr := flag.String("udp-addr", ":39001", "UDP discovery listen address")
	tlsAddr := flag.String("tls-addr", ":39002", "TLS client listen address")
	identityDir := flag.String("identity-dir", "", "directory for the persistent server identity (default: data beside executable)")
	flag.Parse()
	identityPath := *identityDir
	if identityPath == "" {
		// executable, executableErr := os.Executable()
		// if executableErr == nil && !strings.HasPrefix(filepath.Clean(executable), filepath.Clean(os.TempDir())+string(os.PathSeparator)) {
		// 	identityPath = filepath.Join(filepath.Dir(executable), "data")
		// } else {
		// 	// `go run` places the temporary executable under os.TempDir(); use
		// 	// the project root so running from a subdirectory still reuses it.
		// }
		identityPath = "data"
	}
	if err := os.MkdirAll(identityPath, 0o700); err != nil {
		logError := slog.New(slog.NewTextHandler(os.Stderr, nil))
		logError.Error("create identity directory", "error", err)
		return
	}

	logFile, err := os.OpenFile(filepath.Join(identityPath, "server.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		logger := slog.New(slog.NewTextHandler(os.Stderr, nil))
		logger.Error("open server log", "error", err)
		return
	}
	defer logFile.Close()
	logger := slog.New(slog.NewJSONHandler(io.MultiWriter(os.Stdout, logEventWriter{writer: logFile}), &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)
	serverIdentity, err := identity.LoadOrCreate(identityPath, "lovemilk-class-broadcaster", time.Now())
	if err != nil {
		logger.Error("create server identity", "error", err)
		return
	}
	serverPreferences, err := openServerPreferences(filepath.Join(identityPath, "server.db"))
	if err != nil {
		logger.Error("load server preferences", "error", err)
		return
	}
	persistedSnapshot, err := serverPreferences.loadConfigSnapshot(time.Now())
	if err != nil {
		logger.Error("load config snapshot", "error", err)
		return
	}
	configManager, err := config.NewManagerFromSnapshot(persistedSnapshot)
	if err != nil {
		logger.Error("create config manager", "error", err)
		return
	}
	configManager.SetPersistence(serverPreferences.saveConfigSnapshot)
	if err := serverPreferences.saveConfigSnapshot(persistedSnapshot); err != nil {
		logger.Error("persist config snapshot", "error", err)
		return
	}
	messageStore, err := broadcast.NewPersistentStore(filepath.Join(identityPath, "server.db"))
	if err != nil {
		logger.Error("load persistent message store", "error", err)
		return
	}
	deviceRegistry, err := session.NewPersistentRegistry(filepath.Join(identityPath, "server.db"))
	if err != nil {
		logger.Error("load device registry", "error", err)
		return
	}
	sessionHub := session.NewHub()
	shutdownRequest := make(chan struct{}, 1)
	requestShutdown := func() {
		select {
		case shutdownRequest <- struct{}{}:
		default:
		}
	}

	server := &http.Server{
		Addr:              *httpAddr,
		Handler:           newRouterWithDependencies(messageStore, deviceRegistry, sessionHub, configManager, serverPreferences, requestShutdown, filepath.Join(identityPath, "server.log")),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		// The management API includes a long-lived SSE endpoint; a server-wide
		// write timeout would silently terminate every event stream after 15s.
		IdleTimeout: 60 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() {
		select {
		case <-shutdownRequest:
			stop()
		case <-ctx.Done():
		}
	}()

	serverErr := make(chan error, 1)
	udpSocket, err := net.ResolveUDPAddr("udp4", *udpAddr)
	if err != nil {
		logger.Error("resolve UDP address", "error", err)
		return
	}
	udpConn, err := net.ListenUDP("udp4", udpSocket)
	if err != nil {
		logger.Error("listen UDP discovery", "addr", *udpAddr, "error", err)
		return
	}
	go func() {
		defer udpConn.Close()
		logger.Info("UDP discovery listening", "addr", udpConn.LocalAddr())
		if err := (discovery.Responder{Identity: serverIdentity, ServiceName: "lovemilk-class-broadcaster", TCPPort: portOf(*tlsAddr), HTTPPort: portOf(*httpAddr)}).Serve(ctx, udpConn); err != nil && !errors.Is(err, net.ErrClosed) {
			serverErr <- err
		}
	}()

	tlsCertificate, err := serverIdentity.TLSCertificate()
	if err != nil {
		logger.Error("create TLS certificate", "error", err)
		return
	}
	tlsListener, err := net.Listen("tcp", *tlsAddr)
	if err != nil {
		logger.Error("listen TLS", "addr", *tlsAddr, "error", err)
		return
	}
	go func() {
		defer tlsListener.Close()
		listener := tls.NewListener(tlsListener, newTLSConfig(tlsCertificate))
		logger.Info("TLS client listening", "addr", tlsListener.Addr())
		for {
			conn, acceptErr := listener.Accept()
			if acceptErr != nil {
				select {
				case <-ctx.Done():
					return
				default:
				}
				serverErr <- acceptErr
				return
			}
			go serveTLSConnection(ctx, conn, serverIdentity, configManager, deviceRegistry, sessionHub, messageStore)
		}
	}()
	go dispatchMessages(ctx, messageStore, sessionHub, configManager)

	go func() {
		logger.Info("management API listening", "addr", *httpAddr, "framework", "gin")
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			serverErr <- err
		}
	}()

	select {
	case <-ctx.Done():
		shutdownPacket := protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.ServerShutdown, 0, 0, nil)
		for _, clientID := range sessionHub.OnlineIDs() {
			_ = sessionHub.Send(clientID, shutdownPacket)
		}
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := server.Shutdown(shutdownCtx); err != nil {
			logger.Error("management API shutdown failed", "error", err)
			os.Exit(1)
		}
	case err := <-serverErr:
		if err != nil {
			logger.Error("management API stopped", "error", err)
			os.Exit(1)
		}
	}
}

func newTLSConfig(certificate tls.Certificate) *tls.Config {
	return &tls.Config{
		MinVersion:   tls.VersionTLS13,
		Certificates: []tls.Certificate{certificate},
		ClientAuth:   tls.RequireAnyClientCert,
	}
}

func serveTLSConnection(ctx context.Context, raw net.Conn, serverIdentity identity.Identity, configManager *config.Manager, registry *session.Registry, hub *session.Hub, store *broadcast.Store) {
	defer raw.Close()
	conn, ok := raw.(*tls.Conn)
	if !ok {
		return
	}
	if err := conn.HandshakeContext(ctx); err != nil {
		slog.Warn("TLS handshake failed", "error", err)
		return
	}
	state := conn.ConnectionState()
	if len(state.PeerCertificates) == 0 {
		return
	}
	clientEdKey, ok := state.PeerCertificates[0].PublicKey.(ed25519.PublicKey)
	if !ok {
		slog.Warn("client certificate is not Ed25519")
		return
	}
	clientID, clientIDErr := session.ClientID(clientEdKey)
	if clientIDErr != nil {
		slog.Warn("calculate client fingerprint failed", "error", clientIDErr)
		return
	}
	if !registry.IsApproved(clientEdKey) {
		_, observeErr := registry.ObservePending(clientEdKey, time.Now())
		if observeErr != nil {
			slog.Warn("record pending client failed", "client_id", clientID, "error", observeErr)
		}
		managementEvents.publish("devices")
		slog.Warn("client rejected", "client_id", clientID, "reason", "client_not_approved")
		payload, _ := protocol.MarshalBSON(map[string]any{"code": "client_not_approved", "message": "client public key is pending approval", "client_id": clientID, "retryable": true})
		_ = protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.Error, 0, 0, payload).WritePacket(conn)
		return
	}
	packet, err := protocol.ReadPacket(conn)
	if err != nil || packet.Type != protocol.ConnectReq {
		slog.Warn("invalid connect request", "client_id", clientID, "error", err, "packet_type", packet.Type)
		return
	}
	var request session.ConnectRequest
	if err := protocol.UnmarshalBSON(packet.Payload, &request); err != nil {
		slog.Warn("decode connect request failed", "client_id", clientID, "error", err)
		return
	}
	if err := session.ValidateRequest(request, clientEdKey); err != nil {
		slog.Warn("validate connect request failed", "client_id", clientID, "error", err)
		return
	}
	registry.MarkSeen(clientEdKey, time.Now())
	response, err := session.BuildResponse(request, serverIdentity.PublicKey, configManager.Current(), time.Now())
	if err != nil {
		return
	}
	payload, err := protocol.MarshalBSON(response)
	if err != nil {
		return
	}
	if err := protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.ConnectResp, packet.Seq, 0, payload).WritePacket(conn); err != nil {
		return
	}
	unregister := hub.Register(clientID, conn)
	managementEvents.publish("devices")
	defer func() {
		unregister()
		managementEvents.publish("devices")
	}()
	configEvents, unsubscribe := configManager.Subscribe()
	defer unsubscribe()
	connectionConfigID := configManager.Current().ID
	sendConfigUpdate := func(updated config.Snapshot) bool {
		configPayload, marshalErr := protocol.MarshalBSON(updated)
		if marshalErr != nil {
			return false
		}
		if hub.Send(clientID, protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.ConfigUpdate, 0, 0, configPayload)) != nil {
			return false
		}
		connectionConfigID = updated.ID
		return true
	}
	for {
		select {
		case updated, ok := <-configEvents:
			if !ok {
				return
			}
			if !sendConfigUpdate(updated) {
				return
			}
		default:
		}
		_ = conn.SetReadDeadline(time.Now().Add(time.Second))
		packet, err := protocol.ReadPacket(conn)
		if err != nil {
			if timeout, ok := err.(net.Error); ok && timeout.Timeout() {
				select {
				case <-ctx.Done():
					return
				default:
					continue
				}
			}
			if errors.Is(err, io.EOF) {
				return
			}
			return
		}
		if packet.Type == protocol.Ping {
			current := configManager.Current()
			if connectionConfigID != current.ID && !sendConfigUpdate(current) {
				return
			}
			_ = hub.Send(clientID, protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.Pong, packet.Seq, 0, packet.Payload))
		} else if packet.Type == protocol.MessageAck {
			var acknowledgement messageAck
			if protocol.UnmarshalBSON(packet.Payload, &acknowledgement) == nil {
				if messageID, parseErr := broadcast.ParseMessageID(acknowledgement.MessageID); parseErr == nil {
					if store.Acknowledge(messageID, clientID, broadcast.DeliveryStatus(acknowledgement.Status)) {
						slog.Info("message delivery acknowledgement", "message_id", acknowledgement.MessageID, "client_id", clientID, "status", acknowledgement.Status)
						managementEvents.publish("messages")
					}
				}
			}
		}
	}
}

type messageEnvelope struct {
	MessageID                   string                 `bson:"message_id"`
	QueueSeq                    uint64                 `bson:"queue_seq"`
	Priority                    uint16                 `bson:"priority"`
	Content                     string                 `bson:"content"`
	CreatedAt                   int64                  `bson:"created_at"`
	ExpiresAt                   int64                  `bson:"expires_at"`
	DisplayPosition             *string                `bson:"display_position,omitempty"`
	DisplayDurationRatio        *float64               `bson:"display_duration_ratio,omitempty"`
	AsDefault                   bool                   `bson:"as_default"`
	DefaultDisplayPosition      string                 `bson:"default_display_position,omitempty"`
	DefaultDisplayDurationRatio float64                `bson:"default_display_duration_ratio"`
	Speech                      []broadcast.SpeechNode `bson:"speech,omitempty"`
}

type messageAck struct {
	MessageID string `bson:"message_id"`
	Status    string `bson:"status"`
	Error     string `bson:"error,omitempty"`
}

func dispatchMessages(ctx context.Context, store *broadcast.Store, hub *session.Hub, configManager *config.Manager) {
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			if store.Expire(now) > 0 {
				managementEvents.publish("messages")
			}
			record := store.NextPending(now)
			if record == nil {
				continue
			}
			targets := append([]string(nil), record.TargetClients...)
			if len(targets) == 0 {
				targets = hub.OnlineIDs()
			}
			if len(targets) == 0 {
				_ = store.Retry(record.Message.ID)
				continue
			}
			currentConfig := configManager.Current()
			payload, err := protocol.MarshalBSON(messageEnvelope{
				MessageID: record.Message.ID.String(), QueueSeq: record.Message.QueueSeq,
				Priority: record.Message.Priority,
				Content:  record.Message.DisplayText, CreatedAt: record.CreatedAt.UnixMilli(),
				ExpiresAt: record.ExpiresAt.UnixMilli(), Speech: record.Message.Speech,
				DisplayPosition:             record.Message.DisplayPosition,
				DisplayDurationRatio:        record.Message.DisplayDurationRatio,
				AsDefault:                   record.Message.DisplayPosition == nil || record.Message.DisplayDurationRatio == nil,
				DefaultDisplayPosition:      currentConfig.DisplayPosition,
				DefaultDisplayDurationRatio: currentConfig.DisplayDurationRatio,
			})
			if err != nil {
				_ = store.Retry(record.Message.ID)
				continue
			}
			packet := protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.Message, 0, 0, payload)
			allSent := true
			messageChanged := false
			for _, target := range targets {
				if err := hub.Send(target, packet); err != nil {
					slog.Warn("message delivery failed", "message_id", record.Message.ID.String(), "client_id", target, "error", err)
					allSent = false
				} else {
					if record.Deliveries[target] != broadcast.DeliverySent {
						messageChanged = true
					}
					if !store.MarkSent(record.Message.ID, target) {
						slog.Warn("message delivery bookkeeping failed", "message_id", record.Message.ID.String(), "client_id", target)
						allSent = false
					}
				}
			}
			if allSent {
				_ = store.Complete(record.Message.ID, now)
			} else {
				_ = store.Retry(record.Message.ID)
			}
			if messageChanged || allSent {
				managementEvents.publish("messages")
			}
		}
	}
}

func notifyWithdrawal(record broadcast.Record, hub *session.Hub) {
	targets := make(map[string]struct{}, len(record.TargetClients)+len(record.Deliveries))
	for _, target := range record.TargetClients {
		if target != "" {
			targets[target] = struct{}{}
		}
	}
	for target := range record.Deliveries {
		targets[target] = struct{}{}
	}
	payload, err := protocol.MarshalBSON(map[string]any{
		"message_id": record.Message.ID.String(), "message": "消息已撤回",
		"content": record.Message.DisplayText,
	})
	if err != nil {
		return
	}
	packet := protocol.New(protocol.ProtocolMajor, protocol.ProtocolMinor, protocol.MessageWithdraw, 0, 0, payload)
	for target := range targets {
		_ = hub.Send(target, packet)
	}
}

func portOf(address string) uint16 {
	_, port, err := net.SplitHostPort(address)
	if err != nil {
		return 0
	}
	var value uint64
	for _, c := range port {
		if c < '0' || c > '9' {
			return 0
		}
		value = value*10 + uint64(c-'0')
		if value > 65535 {
			return 0
		}
	}
	return uint16(value)
}

type createMessageRequest struct {
	MessageID            string                 `json:"message_id"`
	Priority             *uint16                `json:"priority"`
	Content              string                 `json:"content" binding:"required"`
	Speech               []broadcast.SpeechNode `json:"speech"`
	TargetClientIDs      []string               `json:"target_client_ids"`
	ExpiresAt            *time.Time             `json:"expires_at"`
	DisplayPosition      *string                `json:"display_position"`
	DisplayDurationRatio *float64               `json:"display_duration_ratio"`
}

type messageResponse struct {
	MessageID            string                              `json:"message_id"`
	QueueSeq             uint64                              `json:"queue_seq"`
	Priority             uint16                              `json:"priority"`
	Content              string                              `json:"content"`
	TargetClientIDs      []string                            `json:"target_client_ids"`
	Status               broadcast.DeliveryStatus            `json:"status"`
	CreatedAt            int64                               `json:"created_at"`
	ExpiresAt            int64                               `json:"expires_at"`
	DisplayPosition      *string                             `json:"display_position,omitempty"`
	DisplayDurationRatio *float64                            `json:"display_duration_ratio,omitempty"`
	TTSEnabled           bool                                `json:"tts_enabled"`
	Deliveries           map[string]broadcast.DeliveryStatus `json:"deliveries,omitempty"`
}

func newRouter(stores ...*broadcast.Store) *gin.Engine {
	store := broadcast.NewStore()
	if len(stores) > 0 && stores[0] != nil {
		store = stores[0]
	}
	manager, err := config.NewManager(time.Now())
	if err != nil {
		panic(err)
	}
	return newRouterWithConfig(store, session.NewRegistry(), session.NewHub(), manager)
}

type approveDeviceRequest struct {
	ClientID string `json:"client_id" binding:"required"`
	Label    string `json:"label"`
}

type renameDeviceRequest struct {
	Label string `json:"label"`
}

type deviceResponse struct {
	session.Device
	Online bool `json:"online"`
}

type configUpdateRequest struct {
	HeartbeatIntervalSeconds *int32   `json:"heartbeat_interval_seconds"`
	HeartbeatTimeoutSeconds  *int32   `json:"heartbeat_timeout_seconds"`
	MessageTTLHours          *int32   `json:"message_ttl_hours"`
	MaxSpeechDepth           *int32   `json:"max_speech_depth"`
	MaxRepeatExpansion       *int32   `json:"max_repeat_expansion"`
	DisplayPosition          *string  `json:"default_display_position"`
	DisplayDurationRatio     *float64 `json:"default_display_duration_ratio"`
	Immediate                bool     `json:"immediate"`
}

type configResponse struct {
	ConfigID             string  `json:"config_id"`
	IssuedAt             int64   `json:"issued_at"`
	HeartbeatInterval    int64   `json:"heartbeat_interval_seconds"`
	HeartbeatTimeout     int64   `json:"heartbeat_timeout_seconds"`
	MessageTTL           int64   `json:"message_ttl_hours"`
	MaxSpeechDepth       int32   `json:"max_speech_depth"`
	MaxRepeatExpansion   int32   `json:"max_repeat_expansion"`
	DisplayPosition      string  `json:"default_display_position"`
	DisplayDurationRatio float64 `json:"display_duration_ratio"`
}

func newRouterWithConfig(store *broadcast.Store, registry *session.Registry, hub *session.Hub, configManager *config.Manager, logPaths ...string) *gin.Engine {
	return newRouterWithDependencies(store, registry, hub, configManager, nil, nil, logPaths...)
}

func newRouterWithDependencies(store *broadcast.Store, registry *session.Registry, hub *session.Hub, configManager *config.Manager, preferences *serverPreferences, requestShutdown func(), logPaths ...string) *gin.Engine {
	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	router.Use(gin.Recovery())
	logPath := ""
	if len(logPaths) > 0 {
		logPath = logPaths[0]
	}
	router.Use(func(c *gin.Context) {
		started := time.Now()
		c.Next()
		if c.Request.URL.Path != "/api/v1/events" {
			slog.Info("HTTP request", "method", c.Request.Method, "path", c.Request.URL.Path, "status", c.Writer.Status(), "duration_ms", time.Since(started).Milliseconds())
		}
	})
	router.GET("/healthz", func(c *gin.Context) {
		c.JSON(http.StatusOK, healthResponse{Status: "ok", Service: "class-broadcaster"})
	})
	router.GET("/api/v1/preferences", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"page_size": preferences.pageSize()})
	})
	router.POST("/api/v1/preferences", func(c *gin.Context) {
		var input struct {
			PageSize int `json:"page_size" binding:"required"`
		}
		if err := c.ShouldBindJSON(&input); err != nil || !validPageSize(input.PageSize) {
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid page size"})
			return
		}
		if err := preferences.setPageSize(input.PageSize); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
		managementEvents.publish("preferences")
		c.JSON(http.StatusOK, gin.H{"page_size": input.PageSize})
	})
	router.POST("/api/v1/shutdown", func(c *gin.Context) {
		if requestShutdown == nil {
			c.JSON(http.StatusNotImplemented, gin.H{"error": "controlled shutdown is unavailable"})
			return
		}
		requestShutdown()
		c.JSON(http.StatusAccepted, gin.H{"status": "shutdown_requested"})
	})
	router.GET("/api/v1/health", func(c *gin.Context) {
		c.JSON(http.StatusOK, healthResponse{Status: "ok", Service: "class-broadcaster"})
	})
	router.GET("/api/v1/events", func(c *gin.Context) {
		c.Header("Content-Type", "text/event-stream")
		c.Header("Cache-Control", "no-cache")
		c.Header("Connection", "keep-alive")
		c.Header("X-Accel-Buffering", "no")
		flusher, ok := c.Writer.(http.Flusher)
		if !ok {
			c.Status(http.StatusInternalServerError)
			return
		}
		events, unsubscribe := managementEvents.subscribe()
		defer unsubscribe()
		c.Writer.Write([]byte(": connected\n\n"))
		flusher.Flush()
		keepAlive := time.NewTicker(15 * time.Second)
		defer keepAlive.Stop()
		for {
			select {
			case <-c.Request.Context().Done():
				return
			case event, open := <-events:
				if !open {
					return
				}
				_, _ = c.Writer.Write([]byte("event: refresh\ndata: {\"section\":\"" + event + "\"}\n\n"))
				flusher.Flush()
			case <-keepAlive.C:
				_, _ = c.Writer.Write([]byte(": keepalive\n\n"))
				flusher.Flush()
			}
		}
	})
	router.GET("/api/v1/logs", func(c *gin.Context) {
		page := 1
		if value, err := strconv.Atoi(c.Query("page")); err == nil && value > 0 {
			page = value
		}
		pageSize := 100 // 后端默认每页 100 条，前端可以按共享分页偏好请求更小页面。
		if value, err := strconv.Atoi(c.Query("page_size")); err == nil && value > 0 {
			pageSize = value
		}
		if pageSize < 10 || pageSize > 200 {
			c.JSON(http.StatusBadRequest, gin.H{"error": "log page size must be between 10 and 200"})
			return
		}
		content, total, err := readLogPage(logPath, page, pageSize, logPageFilter{
			Start:          c.Query("start"),
			End:            c.Query("end"),
			MinimumLevel:   c.Query("minimum_level"),
			SelectedLevels: c.Query("levels"),
		})
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
		c.JSON(http.StatusOK, gin.H{"content": content, "page": page, "page_size": pageSize, "total": total})
	})
	router.GET("/api/v1/config", func(c *gin.Context) {
		c.JSON(http.StatusOK, toConfigResponse(configManager.Current()))
	})
	router.POST("/api/v1/config", func(c *gin.Context) {
		var input configUpdateRequest
		if err := c.ShouldBindJSON(&input); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		update := config.Update{
			MaxSpeechDepth: input.MaxSpeechDepth, MaxRepeatExpansion: input.MaxRepeatExpansion,
			DisplayPosition: input.DisplayPosition, DisplayDurationRatio: input.DisplayDurationRatio,
		}
		if input.HeartbeatIntervalSeconds != nil {
			value := time.Duration(*input.HeartbeatIntervalSeconds) * time.Second
			update.HeartbeatInterval = &value
		}
		if input.HeartbeatTimeoutSeconds != nil {
			value := time.Duration(*input.HeartbeatTimeoutSeconds) * time.Second
			update.HeartbeatTimeout = &value
		}
		if input.MessageTTLHours != nil {
			value := time.Duration(*input.MessageTTLHours) * time.Hour
			update.MessageTTL = &value
		}
		updated, err := configManager.Update(update, time.Now())
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		if input.Immediate {
			configManager.Publish(updated)
		}
		managementEvents.publish("config")
		c.JSON(http.StatusOK, gin.H{"config": toConfigResponse(updated), "immediate": input.Immediate})
	})
	router.GET("/api/v1/devices", func(c *gin.Context) {
		items := registry.List()
		response := make([]deviceResponse, 0, len(items))
		for _, device := range items {
			response = append(response, deviceResponse{Device: device, Online: hub.Online(device.ClientID)})
		}
		c.JSON(http.StatusOK, gin.H{"items": response})
	})
	router.POST("/api/v1/devices", func(c *gin.Context) {
		var input approveDeviceRequest
		if err := c.ShouldBindJSON(&input); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		if err := session.ValidateClientID(input.ClientID); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		device, err := registry.ApproveClientID(input.ClientID, input.Label, time.Now())
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		managementEvents.publish("devices")
		c.JSON(http.StatusCreated, device)
	})
	router.PATCH("/api/v1/devices/:client_id", func(c *gin.Context) {
		var input renameDeviceRequest
		if err := c.ShouldBindJSON(&input); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		device, err := registry.RenameClientID(c.Param("client_id"), input.Label)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		managementEvents.publish("devices")
		c.JSON(http.StatusOK, device)
	})
	router.DELETE("/api/v1/devices/:client_id", func(c *gin.Context) {
		clientID := c.Param("client_id")
		if err := registry.RevokeClientID(clientID); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		hub.Disconnect(clientID)
		managementEvents.publish("devices")
		c.Status(http.StatusNoContent)
	})
	router.POST("/api/v1/devices/:client_id/disconnect", func(c *gin.Context) {
		clientID, err := session.NormalizeClientID(c.Param("client_id"))
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		hub.Disconnect(clientID)
		managementEvents.publish("devices")
		c.Status(http.StatusNoContent)
	})
	router.GET("/api/v1/devices/:client_id/logs", func(c *gin.Context) {
		clientID, err := session.NormalizeClientID(c.Param("client_id"))
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		content, err := readLogTail(logPath, 5000)
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
		lines := strings.Split(content, "\n")
		matched := make([]string, 0, 200)
		for _, line := range lines {
			var entry map[string]any
			if json.Unmarshal([]byte(line), &entry) == nil && entry["client_id"] == clientID {
				matched = append(matched, line)
			}
		}
		if len(matched) > 500 {
			matched = matched[len(matched)-500:]
		}
		c.JSON(http.StatusOK, gin.H{"content": strings.Join(matched, "\n"), "client_id": clientID})
	})
	router.POST("/api/v1/messages", func(c *gin.Context) {
		var input createMessageRequest
		if err := c.ShouldBindJSON(&input); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		var id broadcast.MessageID
		var err error
		if input.MessageID == "" {
			id, err = broadcast.NewMessageID()
		} else {
			id, err = broadcast.ParseMessageID(input.MessageID)
		}
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		priority := broadcast.DefaultPriority
		if input.Priority != nil {
			priority = *input.Priority
		}
		now := time.Now()
		if input.DisplayDurationRatio != nil && (*input.DisplayDurationRatio < 0 || *input.DisplayDurationRatio > 60) {
			c.JSON(http.StatusBadRequest, gin.H{"error": "display duration ratio must be between 0 and 60"})
			return
		}
		if input.DisplayPosition != nil && !config.ValidDisplayPosition(*input.DisplayPosition) {
			c.JSON(http.StatusBadRequest, gin.H{"error": "display position is invalid"})
			return
		}
		message := broadcast.Message{
			ID: id, Priority: priority, PrioritySet: input.Priority != nil,
			DisplayText: input.Content, Speech: input.Speech,
			DisplayPosition: input.DisplayPosition, DisplayDurationRatio: input.DisplayDurationRatio,
		}
		if input.ExpiresAt != nil {
			message.ExpiresAt = input.ExpiresAt.UnixMilli()
		}
		targetClientIDs := make([]string, 0, len(input.TargetClientIDs))
		for _, value := range input.TargetClientIDs {
			normalized, normalizeErr := session.NormalizeClientID(value)
			if normalizeErr != nil {
				// Keep the store/API compatible with non-fingerprint identifiers used
				// by integrations; fingerprint targets are normalized canonically.
				targetClientIDs = append(targetClientIDs, strings.TrimSpace(value))
				continue
			}
			targetClientIDs = append(targetClientIDs, normalized)
		}
		record, err := store.Create(message, targetClientIDs, now)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		managementEvents.publish("messages")
		c.JSON(http.StatusCreated, toMessageResponse(record))
	})
	router.GET("/api/v1/messages", func(c *gin.Context) {
		items := store.List(time.Now())
		response := make([]messageResponse, 0, len(items))
		for _, item := range items {
			response = append(response, toMessageResponse(item))
		}
		c.JSON(http.StatusOK, gin.H{"items": response, "warnings": store.Warnings()})
	})
	router.POST("/api/v1/messages/:message_id/withdraw", func(c *gin.Context) {
		messageID, err := broadcast.ParseMessageID(c.Param("message_id"))
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		record, ok := store.Withdraw(messageID, time.Now())
		if !ok {
			c.JSON(http.StatusConflict, gin.H{"error": "message cannot be withdrawn"})
			return
		}
		notifyWithdrawal(record, hub)
		managementEvents.publish("messages")
		c.JSON(http.StatusOK, toMessageResponse(record))
	})
	return router
}

func toConfigResponse(snapshot config.Snapshot) configResponse {
	return configResponse{
		ConfigID: snapshot.ID, IssuedAt: snapshot.IssuedAt,
		HeartbeatInterval: int64(snapshot.HeartbeatInterval / time.Second),
		HeartbeatTimeout:  int64(snapshot.HeartbeatTimeout / time.Second),
		MessageTTL:        int64(snapshot.MessageTTL / time.Hour), MaxSpeechDepth: snapshot.MaxSpeechDepth,
		MaxRepeatExpansion: snapshot.MaxRepeatExpansion, DisplayPosition: snapshot.DisplayPosition,
		DisplayDurationRatio: snapshot.DisplayDurationRatio,
	}
}

func readLogTail(path string, limit int) (string, error) {
	if path == "" {
		return "", nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return "", nil
		}
		return "", err
	}
	lines := strings.Split(string(data), "\n")
	if len(lines) > limit+1 {
		lines = lines[len(lines)-(limit+1):]
	}
	return strings.Join(lines, "\n"), nil
}

type logPageFilter struct {
	Start          string
	End            string
	MinimumLevel   string
	SelectedLevels string
}

// readLogPage 在服务端先按条件筛选并分页，避免浏览器一次性解析整个 JSONL 文件。
func readLogPage(path string, page, pageSize int, filter logPageFilter) (string, int, error) {
	if path == "" {
		return "", 0, nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return "", 0, nil
		}
		return "", 0, err
	}
	selected := make(map[string]struct{})
	for _, value := range strings.Split(filter.SelectedLevels, ",") {
		if value = strings.ToUpper(strings.TrimSpace(value)); value != "" {
			selected[value] = struct{}{}
		}
	}
	minimum := logLevelRank(filter.MinimumLevel)
	start := logDateBound(filter.Start, false)
	end := logDateBound(filter.End, true)
	lines := strings.Split(string(data), "\n")
	matched := make([]string, 0, len(lines))
	for _, line := range lines {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		var entry map[string]any
		if json.Unmarshal([]byte(line), &entry) != nil {
			if minimum > 0 || len(selected) > 0 || !start.IsZero() || !end.IsZero() {
				continue
			}
			matched = append(matched, line)
			continue
		}
		level := strings.ToUpper(strings.TrimSpace(fmt.Sprint(entry["level"])))
		if minimum >= 0 && logLevelRank(level) < minimum {
			continue
		}
		if len(selected) > 0 {
			if _, ok := selected[level]; !ok {
				continue
			}
		}
		if timestamp, ok := entry["time"].(string); ok {
			parsed, parseErr := time.Parse(time.RFC3339Nano, timestamp)
			if parseErr == nil && ((!start.IsZero() && parsed.Before(start)) || (!end.IsZero() && !parsed.Before(end))) {
				continue
			}
		}
		matched = append(matched, line)
	}
	// 日志表按最新记录在前展示，页码 1 也始终代表最新一页。
	for left, right := 0, len(matched)-1; left < right; left, right = left+1, right-1 {
		matched[left], matched[right] = matched[right], matched[left]
	}
	total := len(matched)
	if page < 1 {
		page = 1
	}
	startIndex := (page - 1) * pageSize
	if startIndex >= total {
		return "", total, nil
	}
	endIndex := startIndex + pageSize
	if endIndex > total {
		endIndex = total
	}
	return strings.Join(matched[startIndex:endIndex], "\n"), total, nil
}

func logLevelRank(value string) int {
	switch strings.ToUpper(strings.TrimSpace(value)) {
	case "DEBUG":
		return 0
	case "INFO":
		return 1
	case "WARN", "WARNING":
		return 2
	case "ERROR":
		return 3
	default:
		return -1
	}
}

func logDateBound(value string, end bool) time.Time {
	if value == "" {
		return time.Time{}
	}
	location, err := time.LoadLocation("Asia/Taipei")
	if err != nil {
		location = time.FixedZone("Asia/Taipei", 8*60*60)
	}
	date, err := time.ParseInLocation("2006-01-02", value, location)
	if err != nil {
		return time.Time{}
	}
	if end {
		return date.AddDate(0, 0, 1)
	}
	return date
}

func toMessageResponse(record broadcast.Record) messageResponse {
	return messageResponse{
		MessageID: record.Message.ID.String(), QueueSeq: record.Message.QueueSeq,
		Priority: record.Message.Priority, Content: record.Message.DisplayText,
		TargetClientIDs: append([]string(nil), record.TargetClients...), Status: record.Status,
		CreatedAt: record.CreatedAt.UnixMilli(), ExpiresAt: record.ExpiresAt.UnixMilli(),
		DisplayPosition:      record.Message.DisplayPosition,
		DisplayDurationRatio: record.Message.DisplayDurationRatio,
		TTSEnabled:           len(record.Message.Speech) > 0,
		Deliveries:           cloneDeliveryStatuses(record.Deliveries),
	}
}

func cloneDeliveryStatuses(input map[string]broadcast.DeliveryStatus) map[string]broadcast.DeliveryStatus {
	if len(input) == 0 {
		return nil
	}
	output := make(map[string]broadcast.DeliveryStatus, len(input))
	for clientID, status := range input {
		output[clientID] = status
	}
	return output
}
