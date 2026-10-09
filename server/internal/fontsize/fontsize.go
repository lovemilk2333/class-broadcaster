// Package fontsize validates admin/client font-size expressions.
//
// Supported forms (case-insensitive unit):
//
//	"" / "0" / "(默认)" / "(default)"  → empty (client uses built-in 25vh)
//	"38" / "38px"                     → absolute pixels (16–120)
//	"1.2em"                           → relative to 38px (1em = 38px)
//	"5vw" / "8vh" / "25vh"            → percent of the active screen width / height (0–100)
package fontsize

import (
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

const (
	// DefaultPx is the legacy absolute fallback when a client has no screen metrics.
	// The real built-in default expression is "25vh" (resolved on the client).
	DefaultPx = 38
	MinPx     = 16
	MaxPx     = 120
)

var exprPattern = regexp.MustCompile(`(?i)^\s*(\d+(?:\.\d+)?)\s*(px|em|vw|vh)?\s*$`)

// Normalize trims and canonicalizes a font-size expression.
// Empty means “use client built-in / device default” depending on call site.
func Normalize(raw string) (string, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" || raw == "0" || raw == "(默认)" || strings.EqualFold(raw, "(default)") || strings.EqualFold(raw, "default") {
		return "", nil
	}
	match := exprPattern.FindStringSubmatch(raw)
	if match == nil {
		return "", errors.New(`font size must be empty, or like "38px", "1.2em", "5vw", "8vh"`)
	}
	value, err := strconv.ParseFloat(match[1], 64)
	if err != nil || value < 0 {
		return "", errors.New("font size value is invalid")
	}
	unit := strings.ToLower(strings.TrimSpace(match[2]))
	if unit == "" {
		unit = "px"
	}
	if value == 0 {
		return "", nil
	}
	switch unit {
	case "px":
		if value < MinPx || value > MaxPx {
			return "", fmt.Errorf("px font size must be between %d and %d", MinPx, MaxPx)
		}
		// Prefer integer px when whole.
		if value == float64(int(value)) {
			return fmt.Sprintf("%dpx", int(value)), nil
		}
		return trimFloat(value) + "px", nil
	case "em":
		if value < 0.25 || value > 4 {
			return "", errors.New("em font size must be between 0.25 and 4")
		}
		return trimFloat(value) + "em", nil
	case "vw", "vh":
		if value < 0 || value > 100 {
			return "", errors.New(unit + " font size must be between 0 and 100")
		}
		return trimFloat(value) + unit, nil
	default:
		return "", errors.New("unsupported font size unit")
	}
}

func trimFloat(value float64) string {
	text := strconv.FormatFloat(value, 'f', 4, 64)
	text = strings.TrimRight(text, "0")
	text = strings.TrimRight(text, ".")
	if text == "" || text == "-" {
		return "0"
	}
	return text
}
