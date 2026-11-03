// Package config stores the CLI login in the user config directory; <APP>_URL and
// <APP>_TOKEN override the stored values and are never written back.
package config

import (
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

// Lock takes the renewal lock file next to the config and returns its release.
// A lock older than staleAfter is taken over.
func (s *Store) Lock(wait time.Duration) (func(), error) {
	const staleAfter = 30 * time.Second
	if err := os.MkdirAll(filepath.Dir(s.Path), 0o700); err != nil {
		return nil, err
	}
	path := s.Path + ".lock"
	deadline := time.Now().Add(wait)
	for {
		f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
		if err == nil {
			f.Close()
			return func() { os.Remove(path) }, nil
		}
		if !errors.Is(err, fs.ErrExist) {
			return nil, err
		}
		if st, serr := os.Stat(path); serr == nil && time.Since(st.ModTime()) > staleAfter {
			os.Remove(path)
			continue
		}
		if time.Now().After(deadline) {
			return nil, errors.New("timed out waiting for the renewal lock")
		}
		time.Sleep(20 * time.Millisecond)
	}
}
