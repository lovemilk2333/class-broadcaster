package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestCleanupUpdateStagingRemovesUpdatesDir(t *testing.T) {
	root := t.TempDir()
	updates := filepath.Join(root, "data", "updates")
	applyDir := filepath.Join(updates, "apply", "abcdef0123456789")
	if err := os.MkdirAll(applyDir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(applyDir, "pkg.tar.zst"), []byte("pkg"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(updates, "deadbeef.tar.zst"), []byte("cached"), 0o600); err != nil {
		t.Fatal(err)
	}
	// Sibling state that must survive.
	if err := os.WriteFile(filepath.Join(root, "data", "state.json"), []byte(`{"version":1}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "data", "client.log"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}

	cleanupUpdateStaging(root)

	if _, err := os.Stat(updates); !os.IsNotExist(err) {
		t.Fatalf("data/updates still present: %v", err)
	}
	for _, name := range []string{"state.json", "client.log"} {
		if _, err := os.Stat(filepath.Join(root, "data", name)); err != nil {
			t.Fatalf("%s missing after cleanup: %v", name, err)
		}
	}
}

func TestCleanupUpdateStagingMissingIsNoop(t *testing.T) {
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "data"), 0o700); err != nil {
		t.Fatal(err)
	}
	cleanupUpdateStaging(root) // must not panic
}
