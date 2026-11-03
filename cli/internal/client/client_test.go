package client

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/dasganni/agentkit/cli/internal/config"
)

type fake struct {
	srv       *httptest.Server
	refreshes atomic.Int32
	valid     string // the access token the API accepts
	protocol  atomic.Value
}

func newFake(t *testing.T, valid string) *fake {
	t.Helper()
	f := &fake{valid: valid}
	mux := http.NewServeMux()
	mux.HandleFunc("/.well-known/oauth-authorization-server", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"authorization_endpoint":"` + f.srv.URL + `/a","token_endpoint":"` + f.srv.URL + `/oauth/token"}`))
	})
	// The refresh token is single use: only the first exchange succeeds.
	mux.HandleFunc("/oauth/token", func(w http.ResponseWriter, _ *http.Request) {
		n := f.refreshes.Add(1)
		time.Sleep(50 * time.Millisecond)
		if n > 1 {
			w.WriteHeader(http.StatusBadRequest)
			_, _ = w.Write([]byte(`{"error":"invalid_grant"}`))
			return
		}
		_, _ = w.Write([]byte(`{"access_token":"fresh","refresh_token":"rt-new","expires_in":3600}`))
	})
	mux.HandleFunc("/api/v1/agent/ping", func(w http.ResponseWriter, r *http.Request) {
		f.protocol.Store(r.Header.Get("Agent-Protocol"))
		if r.Header.Get("Authorization") != "Bearer "+f.valid {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		_, _ = w.Write([]byte(`{"ok":true}`))
	})
	mux.HandleFunc("/api/v1/agent/deny", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	})
	mux.HandleFunc("/api/v1/agent/old", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUpgradeRequired)
	})
	f.srv = httptest.NewServer(mux)
	t.Cleanup(f.srv.Close)
	return f
}

func store(t *testing.T, f *fake, c config.Config) *config.Store {
	t.Helper()
	t.Setenv("TESTAPP_TOKEN", "")
	t.Setenv("TESTAPP_URL", "")
	s := &config.Store{App: "testapp", Path: filepath.Join(t.TempDir(), "config.json")}
	c.URL = f.srv.URL
	c.ClientID = "cid"
	if err := s.Save(&c); err != nil {
		t.Fatal(err)
	}
	return s
}

func get(s *config.Store, path string) (*Response, error) {
	return (&Client{Store: s}).Do(context.Background(), http.MethodGet, path, nil)
}

func TestHeadersAndNoRenewWhenValid(t *testing.T) {
	f := newFake(t, "tok")
	s := store(t, f, config.Config{AccessToken: "tok", RefreshToken: "rt", ExpiresAt: time.Now().Add(time.Hour)})
	resp, err := get(s, "/api/v1/agent/ping")
	if err != nil || resp.Status != 200 || f.refreshes.Load() != 0 || f.protocol.Load() != "1" {
		t.Fatalf("%v %+v refreshes=%d proto=%v", err, resp, f.refreshes.Load(), f.protocol.Load())
	}
}

func TestRenewsBeforeExpiry(t *testing.T) {
	f := newFake(t, "fresh")
	s := store(t, f, config.Config{AccessToken: "old", RefreshToken: "rt", ExpiresAt: time.Now().Add(30 * time.Second)})
	resp, err := get(s, "/api/v1/agent/ping")
	if err != nil || resp.Status != 200 || f.refreshes.Load() != 1 {
		t.Fatalf("%v %+v refreshes=%d", err, resp, f.refreshes.Load())
	}
	c, _ := s.Load()
	if c.AccessToken != "fresh" || c.RefreshToken != "rt-new" {
		t.Fatalf("not stored: %+v", c)
	}
}

func TestRenewsOnceAfter401(t *testing.T) {
	f := newFake(t, "fresh")
	s := store(t, f, config.Config{AccessToken: "revoked", RefreshToken: "rt", ExpiresAt: time.Now().Add(time.Hour)})
	resp, err := get(s, "/api/v1/agent/ping")
	if err != nil || resp.Status != 200 || f.refreshes.Load() != 1 {
		t.Fatalf("%v %+v refreshes=%d", err, resp, f.refreshes.Load())
	}
}

func TestStill401AfterRenewIsAuthError(t *testing.T) {
	f := newFake(t, "fresh")
	s := store(t, f, config.Config{AccessToken: "revoked", RefreshToken: "rt", ExpiresAt: time.Now().Add(time.Hour)})
	_, err := get(s, "/api/v1/agent/deny")
	var ae *AuthError
	if !errors.As(err, &ae) || ae.ExitCode() != 3 || f.refreshes.Load() != 1 {
		t.Fatalf("%v refreshes=%d", err, f.refreshes.Load())
	}
}

func TestConcurrentRenewHoldsLock(t *testing.T) {
	f := newFake(t, "fresh")
	s := store(t, f, config.Config{AccessToken: "revoked", RefreshToken: "rt", ExpiresAt: time.Now().Add(time.Hour)})
	var wg sync.WaitGroup
	errs := make(chan error, 4)
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			resp, err := get(s, "/api/v1/agent/ping")
			if err == nil && resp.Status != 200 {
				err = errors.New("unexpected status")
			}
			errs <- err
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	if n := f.refreshes.Load(); n != 1 {
		t.Fatalf("refresh token used %d times", n)
	}
}

func TestEnvTokenNeverRenewed(t *testing.T) {
	f := newFake(t, "fresh")
	s := store(t, f, config.Config{RefreshToken: "rt", ExpiresAt: time.Now().Add(-time.Hour)})
	t.Setenv("TESTAPP_TOKEN", "envtok")
	_, err := get(s, "/api/v1/agent/ping")
	var ae *AuthError
	if !errors.As(err, &ae) || f.refreshes.Load() != 0 {
		t.Fatalf("%v refreshes=%d", err, f.refreshes.Load())
	}
}

func TestReusedRefreshTokenLogsOut(t *testing.T) {
	f := newFake(t, "fresh")
	f.refreshes.Store(1) // the server already consumed this refresh token
	s := store(t, f, config.Config{AccessToken: "revoked", RefreshToken: "rt", ExpiresAt: time.Now().Add(time.Hour)})
	_, err := get(s, "/api/v1/agent/ping")
	var ae *AuthError
	if !errors.As(err, &ae) {
		t.Fatalf("%v", err)
	}
	if c, _ := s.Load(); c.RefreshToken != "" || c.AccessToken != "" {
		t.Fatalf("login not cleared: %+v", c)
	}
}

func TestUpgradeRequired(t *testing.T) {
	f := newFake(t, "tok")
	s := store(t, f, config.Config{AccessToken: "tok", ExpiresAt: time.Now().Add(time.Hour)})
	_, err := get(s, "/api/v1/agent/old")
	var pe *ProtocolError
	if !errors.As(err, &pe) {
		t.Fatalf("%v", err)
	}
}
