// Package version holds the server release identity.
//
// Canonical source is the repo-root version.json key "server" (client uses
// "client"). The Makefile projects that key into this package's version.json
// (go:embed) and may inject BuildDate via -ldflags -X at link time.
package version

import (
	_ "embed"
	"encoding/json"
	"strings"
)

//go:embed version.json
var versionJSON []byte

// Version is the semantic release string (e.g. "0.1.0").
// Default is replaced from version.json in init; Makefile may also -X this symbol.
var Version = "0.1.0"

// BuildDate is the stamped build time "yyyy-mm-dd HH:MM:SS.xxxx±xx:xx",
// or "dev" for unstamped builds. Legacy YYYY-MM-DD values are still accepted.
// Prefer a non-empty build_date from version.json; otherwise keep linker -X value.
var BuildDate = "dev"

func init() {
	var document struct {
		Version   string `json:"version"`
		BuildDate string `json:"build_date"`
	}
	if err := json.Unmarshal(versionJSON, &document); err != nil {
		return
	}
	if v := strings.TrimSpace(document.Version); v != "" {
		Version = v
	}
	if d := strings.TrimSpace(document.BuildDate); d != "" && !strings.EqualFold(d, "dev") {
		BuildDate = d
	}
}

// Display returns "x.y.z (yyyy-mm-dd HH:MM:SS.xxxx±xx:xx)" for UI and health.
func Display() string {
	if BuildDate == "" || BuildDate == "dev" {
		return Version + " (dev)"
	}
	return Version + " (" + BuildDate + ")"
}
