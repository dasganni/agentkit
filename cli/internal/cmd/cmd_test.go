package cmd

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/dasganni/agentkit/cli/internal/client"
	"github.com/dasganni/agentkit/cli/internal/config"
	"github.com/dasganni/agentkit/cli/internal/oauth"
	"github.com/dasganni/agentkit/cli/internal/tools"
)

const schema = `[{"name":"notes","description":"Notes.","ops":["list"],"params":{"type":"object",
"properties":{"limit":{"type":"integer"}}}}]`

type env struct {
	app      *App
	out, err *bytes.Buffer
	store    *config.Store
}

func setup(t *testing.T, url string) *env {
	t.Helper()
	t.Setenv("DEMO_URL", "")
	t.Setenv("DEMO_TOKEN", "")
	store := &config.Store{App: "demo", DefaultURL: url, Path: filepath.Join(t.TempDir(), "config.json")}
	list, err := tools.Load([]byte(schema))
	if err != nil {
		t.Fatal(err)
	}
	e := &env{out: &bytes.Buffer{}, err: &bytes.Buffer{}, store: store}
	e.app = &App{
		Name: "demo", Tools: list, Store: store, Client: &client.Client{Store: store},
		Login: func(context.Context, oauth.Options) (*oauth.Tokens, error) {
			return &oauth.Tokens{ClientID: "c", AccessToken: "tok", RefreshToken: "r"}, nil
		},
		In: strings.NewReader(""), Out: e.out, Err: e.err,
	}
	return e
}

func (e *env) login(t *testing.T) {
	t.Helper()
	if err := e.store.Save(&config.Config{AccessToken: "tok"}); err != nil {
		t.Fatal(err)
	}
}

func (e *env) run(args ...string) int { return e.app.Run(context.Background(), args) }

func TestUsageNamesTheApp(t *testing.T) {
	e := setup(t, "")
	if code := e.run("--help"); code != 0 || !strings.Contains(e.out.String(), "Usage: demo ") {
		t.Fatalf("%d %q", code, e.out.String())
	}
}

func TestHelpIsOffline(t *testing.T) {
	e := setup(t, "http://127.0.0.1:1")
	if code := e.run("notes", "--help"); code != 0 || !strings.Contains(e.out.String(), "--limit int") {
		t.Fatalf("%d %q", code, e.out.String())
	}
}

func TestUnknownFlagAndCommandExit2(t *testing.T) {
	e := setup(t, "http://127.0.0.1:1")
	if code := e.run("notes", "list", "--bogus"); code != 2 {
		t.Fatalf("flag: %d", code)
	}
	if code := e.run("bogus"); code != 2 {
		t.Fatalf("command: %d", code)
	}
	if code := e.run(); code != 2 {
		t.Fatalf("none: %d", code)
	}
}

func TestNotLoggedInExit3(t *testing.T) {
	e := setup(t, "http://127.0.0.1:1")
	if code := e.run("notes", "list"); code != 3 {
		t.Fatalf("%d", code)
	}
	if !strings.Contains(e.err.String(), "demo login") {
		t.Fatalf("no hint: %q", e.err.String())
	}
	if code := e.run("status"); code != 3 {
		t.Fatalf("status %d", code)
	}
}

func TestRejectedTokenExit3(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer srv.Close()
	e := setup(t, srv.URL)
	e.login(t)
	if code := e.run("notes", "list"); code != 3 {
		t.Fatalf("%d", code)
	}
}

func TestToolCallTextAndJSON(t *testing.T) {
	var body string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		data, _ := io.ReadAll(r.Body)
		body = string(data)
		if r.URL.Path != "/api/v1/agent/tools/notes" || r.Header.Get("Authorization") != "Bearer tok" {
			t.Errorf("bad request %s %v", r.URL.Path, r.Header)
		}
		_, _ = w.Write([]byte(`{"text":"two notes","images":[]}`))
	}))
	defer srv.Close()
	e := setup(t, srv.URL)
	e.login(t)
	if code := e.run("notes", "list", "--limit", "2"); code != 0 || e.out.String() != "two notes\n" {
		t.Fatalf("%d %q", code, e.out.String())
	}
	if !strings.Contains(body, `"limit":2`) || !strings.Contains(body, `"op":"list"`) {
		t.Fatalf("body %s", body)
	}
	e.out.Reset()
	if code := e.run("--json", "notes", "list"); code != 0 || e.out.String() != `{"text":"two notes","images":[]}`+"\n" {
		t.Fatalf("%d %q", code, e.out.String())
	}
}

func TestServerErrorExit1(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"error":"bad value","field":"limit"}`))
	}))
	defer srv.Close()
	e := setup(t, srv.URL)
	e.login(t)
	if code := e.run("notes", "list"); code != 1 || !strings.Contains(e.err.String(), "bad value (field limit)") {
		t.Fatalf("%d %q", code, e.err.String())
	}
}

func TestLogoutRevokesThenDeletesConfig(t *testing.T) {
	var calls []string
	var e *env
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls = append(calls, r.Method+" "+r.URL.Path)
		if _, err := os.Stat(e.store.Path); err != nil {
			t.Error("config gone before revoke")
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()
	e = setup(t, srv.URL)
	e.login(t)
	if code := e.run("logout"); code != 0 {
		t.Fatalf("%d %q", code, e.err.String())
	}
	if len(calls) != 1 || calls[0] != "DELETE /api/v1/agent/token" {
		t.Fatalf("calls %v", calls)
	}
	if _, err := os.Stat(e.store.Path); !os.IsNotExist(err) {
		t.Fatalf("config still there: %v", err)
	}
}

func TestRevokeFailureKeepsConfig(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()
	e := setup(t, srv.URL)
	e.login(t)
	if code := e.run("token", "revoke"); code != 1 {
		t.Fatalf("%d", code)
	}
	if _, err := os.Stat(e.store.Path); err != nil {
		t.Fatalf("config removed: %v", err)
	}
}

func TestLoginTokenStdin(t *testing.T) {
	e := setup(t, "")
	e.app.In = strings.NewReader("secret-token\n")
	if code := e.run("login", "--url", "http://example.test/", "--token-stdin"); code != 0 {
		t.Fatalf("%d %q", code, e.err.String())
	}
	cfg, err := e.store.Load()
	if err != nil || cfg.AccessToken != "secret-token" || cfg.URL != "http://example.test" {
		t.Fatalf("%+v %v", cfg, err)
	}
	if strings.Contains(e.out.String()+e.err.String(), "secret-token") {
		t.Fatal("token printed")
	}
}

func TestLoginPassesScope(t *testing.T) {
	e := setup(t, "http://example.test")
	var got string
	e.app.Login = func(_ context.Context, o oauth.Options) (*oauth.Tokens, error) {
		got = o.Scope
		return &oauth.Tokens{AccessToken: "tok"}, nil
	}
	if code := e.run("login", "--scope", "read write"); code != 0 || got != "read write" {
		t.Fatalf("%d scope %q", code, got)
	}
	if code := e.run("login"); code != 0 || got != "" {
		t.Fatalf("default scope %q", got)
	}
}

func TestLoginBrowserFlowStoresTokens(t *testing.T) {
	e := setup(t, "http://example.test")
	if code := e.run("login"); code != 0 {
		t.Fatalf("%d %q", code, e.err.String())
	}
	cfg, _ := e.store.Load()
	if cfg.AccessToken != "tok" || cfg.RefreshToken != "r" || cfg.ClientID != "c" {
		t.Fatalf("%+v", cfg)
	}
}

func TestLoginWithoutURLIsUsageError(t *testing.T) {
	e := setup(t, "")
	if code := e.run("login"); code != 2 {
		t.Fatalf("%d", code)
	}
}
