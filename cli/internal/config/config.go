// Package config stores the CLI login in the user config directory; <APP>_URL and
// <APP>_TOKEN override the stored values and are never written back.
package config

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Config is the persisted login.
type Config struct {
	URL          string    `json:"url,omitempty"`
	ClientID     string    `json:"client_id,omitempty"`
	AccessToken  string    `json:"access_token,omitempty"`
	RefreshToken string    `json:"refresh_token,omitempty"`
	ExpiresAt    time.Time `json:"expires_at,omitempty"`

	defaultURL string
	envURL     string
	envToken   string
}

// BaseURL is the env URL, else the stored one, else the build default.
func (c *Config) BaseURL() string {
	for _, u := range []string{c.envURL, c.URL, c.defaultURL} {
		if u != "" {
			return strings.TrimRight(u, "/")
		}
	}
	return ""
}

// Token is the env token, else the stored access token.
func (c *Config) Token() string {
	if c.envToken != "" {
		return c.envToken
	}
	return c.AccessToken
}

// EnvToken reports whether the token comes from the environment (never renewed).
func (c *Config) EnvToken() bool { return c.envToken != "" }

// Store reads and writes one app's config file.
type Store struct {
	App        string
	DefaultURL string
	Path       string
}

// NewStore places the file at os.UserConfigDir()/<app>/config.json.
func NewStore(app, defaultURL string) (*Store, error) {
	dir, err := os.UserConfigDir()
	if err != nil {
		return nil, err
	}
	return &Store{App: app, DefaultURL: defaultURL, Path: filepath.Join(dir, app, "config.json")}, nil
}

func (s *Store) envName(suffix string) string {
	name := strings.ToUpper(strings.NewReplacer("-", "_", ".", "_").Replace(s.App))
	return name + "_" + suffix
}

// Load reads the file (missing is empty) and applies the environment overrides.
func (s *Store) Load() (*Config, error) {
	c := &Config{}
	data, err := os.ReadFile(s.Path)
	switch {
	case err == nil:
		if err := json.Unmarshal(data, c); err != nil {
			return nil, fmt.Errorf("read %s: %w", s.Path, err)
		}
	case !errors.Is(err, fs.ErrNotExist):
		return nil, err
	}
	c.defaultURL = s.DefaultURL
	c.envURL = os.Getenv(s.envName("URL"))
	c.envToken = os.Getenv(s.envName("TOKEN"))
	return c, nil
}

// Save writes the stored fields with mode 0600.
func (s *Store) Save(c *Config) error {
	if err := os.MkdirAll(filepath.Dir(s.Path), 0o700); err != nil {
		return err
	}
	data, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return err
	}
	tmp := s.Path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	if err := os.Chmod(tmp, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, s.Path)
}

// Clear removes the stored login.
func (s *Store) Clear() error {
	if err := os.Remove(s.Path); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	return nil
}

// LockStale is how long a lock may stand before it counts as left by a crashed process. Callers bound
// their work under the lock well below it and wait longer than it, so a live holder is never taken over.
var LockStale = time.Minute

// Lock takes the renewal lock file next to the config and returns its release. The file holds a random
// nonce; release removes it only while it still holds that nonce, so a holder whose lock was taken over
// never deletes the new owner's. A lock older than LockStale is taken over.
//
// limit: two waiters that both find a stale lock can both take it over, only after a crash.
func (s *Store) Lock(wait time.Duration) (func(), error) {
	if err := os.MkdirAll(filepath.Dir(s.Path), 0o700); err != nil {
		return nil, err
	}
	path := s.Path + ".lock"
	nonce, err := newNonce()
	if err != nil {
		return nil, err
	}
	deadline := time.Now().Add(wait)
	for {
		err := createLock(path, nonce)
		if err == nil {
			return func() { releaseLock(path, nonce) }, nil
		}
		if !errors.Is(err, fs.ErrExist) {
			return nil, err
		}
		if st, serr := os.Stat(path); serr == nil && time.Since(st.ModTime()) > LockStale {
			os.Remove(path)
			continue
		}
		if time.Now().After(deadline) {
			return nil, fmt.Errorf("timed out waiting for the renewal lock %s; remove it if no %s command is running", path, s.App)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func newNonce() (string, error) {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

func createLock(path, nonce string) error {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	_, werr := f.WriteString(nonce)
	if cerr := f.Close(); werr == nil {
		werr = cerr
	}
	if werr != nil {
		os.Remove(path)
	}
	return werr
}

func releaseLock(path, nonce string) {
	if b, err := os.ReadFile(path); err == nil && string(b) == nonce {
		os.Remove(path)
	}
}
