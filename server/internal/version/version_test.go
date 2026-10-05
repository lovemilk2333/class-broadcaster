package version

import "testing"

func TestDisplayIncludesBuildDate(t *testing.T) {
	oldVersion, oldDate := Version, BuildDate
	t.Cleanup(func() {
		Version, BuildDate = oldVersion, oldDate
	})
	Version = "1.2.3"
	BuildDate = "2026-10-05 14:30:45.1234+08:00"
	if got := Display(); got != "1.2.3 (2026-10-05 14:30:45.1234+08:00)" {
		t.Fatalf("Display()=%q", got)
	}
	BuildDate = "2026-10-05" // legacy day-only stamp
	if got := Display(); got != "1.2.3 (2026-10-05)" {
		t.Fatalf("Display() legacy=%q", got)
	}
	BuildDate = "dev"
	if got := Display(); got != "1.2.3 (dev)" {
		t.Fatalf("Display()=%q", got)
	}
}
