package fontsize

import "testing"

func TestNormalize(t *testing.T) {
	cases := []struct {
		in, want string
		ok       bool
	}{
		{"", "", true},
		{"0", "", true},
		{"(默认)", "", true},
		{"38", "38px", true},
		{"38px", "38px", true},
		{" 1.2 EM ", "1.2em", true},
		{"5vw", "5vw", true},
		{"8vh", "8vh", true},
		{"0vw", "", true}, // 0 → empty / default
		{"25vh", "25vh", true},
		{"100vw", "100vw", true},
		{"100.1vh", "", false},
		{"10", "", false}, // px below min
		{"200px", "", false},
		{"0.1em", "", false},
		{"abc", "", false},
	}
	for _, tc := range cases {
		got, err := Normalize(tc.in)
		if tc.ok {
			if err != nil {
				t.Fatalf("Normalize(%q) unexpected err: %v", tc.in, err)
			}
			if got != tc.want {
				t.Fatalf("Normalize(%q)=%q want %q", tc.in, got, tc.want)
			}
		} else if err == nil {
			t.Fatalf("Normalize(%q) expected error, got %q", tc.in, got)
		}
	}
}
