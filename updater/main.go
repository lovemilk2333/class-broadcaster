// updater 是独立的跨平台更新替换器。它只处理已由客户端校验过的压缩包，
// 再次检查路径和 metadata 后执行备份、原子替换与失败回滚。
package main

import (
	"archive/tar"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/klauspost/compress/zstd"
)

type updateMetadata struct {
	Component     string   `json:"component"`
	Components    []string `json:"components"`
	Version       string   `json:"version"`
	Platform      string   `json:"platform"`
	SHA256        string   `json:"sha256"`
	PayloadFormat string   `json:"payload_format"`
}

type tarEntry struct {
	name string
	dir  bool
	data []byte
}

type openedPackage struct {
	tar     []tarEntry
	cleanup func()
}

func main() {
	pid := flag.Int("pid", 0, "old process id")
	packagePath := flag.String("package", "", "update package (.tar.zst)")
	installDir := flag.String("install-dir", ".", "installation directory")
	logFile := flag.String("log-file", "", "updater log file (default: <install-dir>/data/updater.log)")
	flag.Parse()
	if *packagePath == "" {
		fatal(errors.New("--package is required"))
	}
	logPath := strings.TrimSpace(*logFile)
	if logPath == "" {
		logPath = filepath.Join(*installDir, "data", "updater.log")
	}
	setupUpdaterLog(logPath)
	updaterLog("info", "updater started identity=%s version=%s build_date=%s package=%s install_dir=%s pid=%d log=%s",
		updaterIdentity, Version, BuildDate, *packagePath, *installDir, *pid, logPath)
	if *pid > 0 {
		updaterLog("info", "waiting for client process exit pid=%d", *pid)
		if err := waitForProcess(*pid); err != nil {
			fatal(err)
		}
		updaterLog("info", "client process exited pid=%d", *pid)
		// Allow the Windows launcher PE, single-instance lock, and Qt/Python DLL
		// unmaps to settle after pythonw exits (Access is denied on Qt6*.dll otherwise).
		time.Sleep(2500 * time.Millisecond)
	}
	if err := applyPackage(*packagePath, *installDir); err != nil {
		fatal(err)
	}
	updaterLog("info", "update apply completed package=%s install_dir=%s", *packagePath, *installDir)
	// Always relaunch so the new binary reconnects and reports the new client_version.
	if err := restartClient(*installDir); err != nil {
		// Install already succeeded; log and exit 0 so operators are not stuck offline forever.
		updaterLog("error", "client restart failed (files updated; start manually): %v", err)
		return
	}
}

func setupUpdaterLog(path string) {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		fmt.Fprintf(os.Stderr, "mkcb.updater: could not create log dir: %v\n", err)
		return
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		fmt.Fprintf(os.Stderr, "mkcb.updater: could not open log file: %v\n", err)
		return
	}
	updaterLogFile = file
}

var updaterLogFile *os.File

// updaterIdentity is the stable logger name uploaded to the server via ClientLog after restart.
const updaterIdentity = "mkcb.updater"

func updaterLog(level, format string, args ...any) {
	message := fmt.Sprintf(format, args...)
	// JSON lines so the restarted client can forward them as structured ClientLog entries
	// with an explicit updater identity (distinct from mkcb.client).
	payload, err := json.Marshal(map[string]any{
		"time":   time.Now().Format(time.RFC3339Nano),
		"level":  strings.ToUpper(level),
		"logger": updaterIdentity,
		"msg":    message,
		"source": "updater",
	})
	line := string(payload) + "\n"
	if err != nil {
		line = fmt.Sprintf(
			`{"time":%q,"level":%q,"logger":%q,"msg":%q,"source":"updater"}`+"\n",
			time.Now().Format(time.RFC3339Nano),
			strings.ToUpper(level),
			updaterIdentity,
			message,
		)
	}
	if updaterLogFile != nil {
		_, _ = updaterLogFile.WriteString(line)
		_ = updaterLogFile.Sync()
	}
	// Human-readable mirror for detached-process stderr / local tailing.
	human := fmt.Sprintf("%s %s %s: %s\n", time.Now().Format("2006-01-02 15:04:05.000"), strings.ToUpper(level), updaterIdentity, message)
	_, _ = os.Stderr.WriteString(human)
}

func applyPackage(packagePath, installDir string) error {
	archive, err := openPackage(packagePath)
	if err != nil {
		return fmt.Errorf("open update package: %w", err)
	}
	defer archive.cleanup()
	metadata, metadataErr := readTarMetadata(archive.tar)
	if metadataErr != nil {
		return metadataErr
	}
	updaterLog("info", "package opened version=%s component=%s sha256=%s", metadata.Version, metadata.Component, metadata.SHA256)
	if err := validateMetadata(metadata); err != nil {
		return err
	}
	if metadata.PayloadFormat != "" && metadata.PayloadFormat != "files-v1" {
		return errors.New("update package payload format is unsupported")
	}
	if err := verifyTarFiles(archive.tar, metadata.SHA256); err != nil {
		return err
	}
	updaterLog("info", "package verified files-v1 sha256=%s", metadata.SHA256)
	// Overlay onto the install tree. Never replace the whole directory: that
	// would wipe runtime state under data/ and break updater-only packages
	// that only ship lovemilk-class-broadcaster-updater.exe.
	if err := overlayInstallation(archive.tar, installDir); err != nil {
		return err
	}
	updaterLog("info", "overlay install finished install_dir=%s", installDir)
	return nil
}

// restartClient launches the installed GUI launcher so the machine reconnects after overlay.
func restartClient(installDir string) error {
	root := filepath.Clean(installDir)
	candidates := []string{
		filepath.Join(root, "lovemilk-class-broadcaster.exe"),
		filepath.Join(root, "lovemilk-class-broadcaster"),
	}
	var launcher string
	for _, candidate := range candidates {
		info, err := os.Stat(candidate)
		if err == nil && !info.IsDir() {
			launcher = candidate
			break
		}
	}
	if launcher == "" {
		return fmt.Errorf("client launcher not found under %s", root)
	}
	// Extra settle: PE replace on Windows can race the dying launcher process.
	time.Sleep(500 * time.Millisecond)
	if err := startDetachedProcess(launcher, root); err != nil {
		return fmt.Errorf("start %s: %w", launcher, err)
	}
	updaterLog("info", "restarted client launcher=%s", launcher)
	return nil
}

func overlayInstallation(entries []tarEntry, installDir string) error {
	root := filepath.Clean(installDir)
	if err := os.MkdirAll(root, 0o700); err != nil {
		return err
	}
	// Stage under a sibling temp dir, then copy file-by-file so a partial
	// failure can leave the previous files intact where possible.
	temporary, err := os.MkdirTemp(filepath.Dir(root), ".mkcb-update-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(temporary)
	if err := extractTar(entries, temporary); err != nil {
		return err
	}
	return copyTreeOverlay(temporary, root)
}

func copyTreeOverlay(source, destination string) error {
	return filepath.Walk(source, func(path string, info os.FileInfo, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		rel, err := filepath.Rel(source, path)
		if err != nil {
			return err
		}
		if rel == "." {
			return nil
		}
		// Never touch the live data directory from package contents named "data".
		// Client state (identity, servers, logs) must survive updates.
		if rel == "data" || strings.HasPrefix(rel, "data"+string(filepath.Separator)) {
			return nil
		}
		target := filepath.Join(destination, rel)
		if info.IsDir() {
			return os.MkdirAll(target, 0o700)
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o700); err != nil {
			return err
		}
		return replaceFile(path, target, info.Mode())
	})
}

func replaceFile(source, destination string, mode os.FileMode) error {
	data, err := os.ReadFile(source)
	if err != nil {
		return err
	}
	// Identical content → skip replace (avoids touching locked Qt/Python DLLs that
	// did not change, which is the common Windows "Access is denied" case).
	if existing, readErr := os.ReadFile(destination); readErr == nil && bytes.Equal(existing, data) {
		return nil
	}
	return replaceFileWithRetry(destination, data, mode)
}

func validateMetadata(metadata updateMetadata) error {
	hasComponents := false
	for _, item := range metadata.Components {
		name := strings.ToLower(strings.TrimSpace(item))
		if name == "client" || name == "updater" {
			hasComponents = true
			break
		}
	}
	if !hasComponents {
		legacy := strings.ToLower(strings.TrimSpace(metadata.Component))
		if legacy != "client" && legacy != "updater" && legacy != "bundle" {
			return errors.New("update metadata is incomplete")
		}
	}
	if metadata.Version == "" {
		return errors.New("update metadata is incomplete")
	}
	if len(metadata.SHA256) != sha256.Size*2 {
		return errors.New("update metadata sha256 is invalid")
	}
	if _, err := hex.DecodeString(metadata.SHA256); err != nil {
		return errors.New("update metadata sha256 is invalid")
	}
	return nil
}

func openPackage(path string) (*openedPackage, error) {
	raw, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	magic := make([]byte, 4)
	_, _ = io.ReadFull(raw, magic)
	if !bytes.Equal(magic, []byte{0x28, 0xb5, 0x2f, 0xfd}) {
		_ = raw.Close()
		// Plain tar is accepted only so unit tests can skip zstd.
		entries, tarErr := readTarEntries(path)
		if tarErr != nil {
			return nil, errors.New("update package must be .tar.zst (zstd-compressed tar)")
		}
		return &openedPackage{tar: entries, cleanup: func() {}}, nil
	}
	_, _ = raw.Seek(0, io.SeekStart)
	decoder, decodeErr := zstd.NewReader(raw)
	if decodeErr != nil {
		_ = raw.Close()
		return nil, decodeErr
	}
	temporary, tempErr := os.CreateTemp("", "mkcb-package-*.tar")
	if tempErr != nil {
		decoder.Close()
		_ = raw.Close()
		return nil, tempErr
	}
	if _, tempErr = io.Copy(temporary, decoder); tempErr != nil {
		decoder.Close()
		_ = temporary.Close()
		_ = raw.Close()
		_ = os.Remove(temporary.Name())
		return nil, tempErr
	}
	decoder.Close()
	_ = raw.Close()
	if tempErr = temporary.Close(); tempErr != nil {
		_ = os.Remove(temporary.Name())
		return nil, tempErr
	}
	rawPath := temporary.Name()
	entries, tarErr := readTarEntries(rawPath)
	_ = os.Remove(rawPath)
	if tarErr != nil {
		return nil, fmt.Errorf("update package is not a valid tar inside zstd: %w", tarErr)
	}
	return &openedPackage{tar: entries, cleanup: func() {}}, nil
}

func readTarEntries(path string) ([]tarEntry, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	reader := tar.NewReader(file)
	entries := make([]tarEntry, 0)
	for {
		header, nextErr := reader.Next()
		if errors.Is(nextErr, io.EOF) {
			return entries, nil
		}
		if nextErr != nil {
			return nil, nextErr
		}
		clean := filepath.Clean(filepath.FromSlash(header.Name))
		if clean == "." || filepath.IsAbs(clean) || clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
			return nil, fmt.Errorf("unsafe archive path %q", header.Name)
		}
		name := filepath.ToSlash(clean)
		switch header.Typeflag {
		case tar.TypeDir:
			entries = append(entries, tarEntry{name: name, dir: true})
		case tar.TypeReg, tar.TypeRegA:
			data, readErr := io.ReadAll(reader)
			if readErr != nil {
				return nil, readErr
			}
			entries = append(entries, tarEntry{name: name, data: data})
		default:
			return nil, fmt.Errorf("unsupported update tar entry %q", header.Name)
		}
	}
}

func readTarMetadata(entries []tarEntry) (updateMetadata, error) {
	for _, entry := range entries {
		if entry.name != "metadata.json" {
			continue
		}
		var metadata updateMetadata
		if err := json.Unmarshal(entry.data, &metadata); err != nil {
			return updateMetadata{}, fmt.Errorf("decode update metadata: %w", err)
		}
		return metadata, nil
	}
	return updateMetadata{}, errors.New("metadata.json is missing")
}

func verifyTarFiles(entries []tarEntry, expected string) error {
	manifestEntries := make([]map[string]interface{}, 0, len(entries))
	seen := make(map[string]struct{})
	for _, entry := range entries {
		if entry.name == "metadata.json" || entry.dir {
			continue
		}
		if _, exists := seen[entry.name]; exists {
			return fmt.Errorf("duplicate update path %q", entry.name)
		}
		seen[entry.name] = struct{}{}
		digest := sha256.Sum256(entry.data)
		manifestEntries = append(manifestEntries, map[string]interface{}{"path": entry.name, "size": len(entry.data), "sha256": hex.EncodeToString(digest[:])})
	}
	sort.Slice(manifestEntries, func(i, j int) bool { return manifestEntries[i]["path"].(string) < manifestEntries[j]["path"].(string) })
	manifest, err := json.Marshal(manifestEntries)
	if err != nil {
		return err
	}
	digest := sha256.Sum256(manifest)
	if hex.EncodeToString(digest[:]) != strings.ToLower(expected) {
		return errors.New("update payload sha256 mismatch")
	}
	return nil
}

func extractTar(entries []tarEntry, destination string) error {
	for _, entry := range entries {
		if entry.name == "metadata.json" {
			continue
		}
		clean := filepath.Clean(filepath.FromSlash(entry.name))
		if clean == "." || filepath.IsAbs(clean) || clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
			return fmt.Errorf("unsafe archive path %q", entry.name)
		}
		path := filepath.Join(destination, clean)
		if entry.dir {
			if err := os.MkdirAll(path, 0o700); err != nil {
				return err
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
			return err
		}
		if err := os.WriteFile(path, entry.data, 0o700); err != nil {
			return err
		}
	}
	return nil
}

func fatal(err error) {
	updaterLog("error", "fatal: %v", err)
	if updaterLogFile != nil {
		_ = updaterLogFile.Close()
	}
	os.Exit(1)
}
