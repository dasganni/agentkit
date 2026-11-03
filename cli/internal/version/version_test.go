package version

import "testing"

func TestConstants(t *testing.T) {
	if Version == "" || Protocol < 1 {
		t.Fatal("version constants unset")
	}
}
