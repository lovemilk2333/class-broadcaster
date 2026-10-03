package config

import (
	"strings"
	"testing"
	"time"
)

func TestConfigIDFreshness(t *testing.T) {
	now := time.UnixMilli(1_700_000_000_000)
	id, err := NewConfigID(now)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, ok := strings.Cut(id, "."); !ok {
		t.Fatalf("config ID has invalid form: %q", id)
	}
	if NeedsRefresh(id, now.Add(6*24*time.Hour), DefaultConfigMaxAge) {
		t.Fatal("fresh config was marked stale")
	}
	if !NeedsRefresh(id, now.Add(8*24*time.Hour), DefaultConfigMaxAge) {
		t.Fatal("stale config was not marked stale")
	}
	if !NeedsRefresh("", now, DefaultConfigMaxAge) {
		t.Fatal("missing config was not marked stale")
	}
	if id == "" {
		t.Fatal("config ID was empty")
	}
	if _, err := ConfigIDIssuedAt(id); err != nil {
		t.Fatalf("generated config ID is invalid: %v", err)
	}
	if !NeedsRefresh("bad.id", now, DefaultConfigMaxAge) {
		t.Fatal("invalid config ID was not marked stale")
	}
}
