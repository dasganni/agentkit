package config

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func newStore(t *testing.T) *Store {
	t.Helper()
	return &Store{App: "my-app", DefaultURL: "https://default.example/", Path: filepath.Join(t.TempDir(), "my-app", "config.json")}
}

func TestSavePermissions(t *testing.T) {
	s := newStore(t)
	if err := s.Save(&Config{AccessToken: "a"}); err != nil {
		t.Fatal(err)
	}
	st, err := os.Stat(s.Path)
	if err != nil {
		t.Fatal(err)
	}
	if st.Mode().Perm() != 0o600 {
		t.Fatalf("mode %v", st.Mode().Perm())
	}
}

func TestLoadMissingUsesDefault(t *testing.T) {
	c, err := newStore(t).Load()
	if err != nil {
		t.Fatal(err)
	}
	if c.BaseURL() != "https://default.example" || c.Token() != "" {
		t.Fatalf("%q %q", c.BaseURL(), c.Token())
	}
}

func TestEnvOverridesAndIsNotPersisted(t *testing.T) {
	s := newStore(t)
	exp := time.Now().Add(time.Hour).UTC().Truncate(time.Second)
	if err := s.Save(&Config{URL: "https://stored.example", AccessToken: "stored", ExpiresAt: exp}); err != nil {
		t.Fatal(err)
	}
	t.Setenv("MY_APP_URL", "https://env.example/")
	t.Setenv("MY_APP_TOKEN", "envtok")
	c, err := s.Load()
	if err != nil {
		t.Fatal(err)
	}
	if c.BaseURL() != "https://env.example" || c.Token() != "envtok" || !c.EnvToken() {
		t.Fatalf("%q %q %v", c.BaseURL(), c.Token(), c.EnvToken())
	}
	if err := s.Save(c); err != nil {
		t.Fatal(err)
	}
	t.Setenv("MY_APP_URL", "")
	t.Setenv("MY_APP_TOKEN", "")
	c, _ = s.Load()
	if c.BaseURL() != "https://stored.example" || c.Token() != "stored" || c.EnvToken() {
		t.Fatalf("env leaked into file: %q %q", c.BaseURL(), c.Token())
	}
}

func TestLockExcludesAndReleases(t *testing.T) {
	s := newStore(t)
	release, err := s.Lock(time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.Lock(60 * time.Millisecond); err == nil {
		t.Fatal("second lock should time out")
	}
	release()
	release2, err := s.Lock(time.Second)
	if err != nil {
		t.Fatal(err)
	}
	release2()
}
