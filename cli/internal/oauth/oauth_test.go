package oauth

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestChallengeRFC7636(t *testing.T) {
	got := Challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
	if got != "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM" {
		t.Fatalf("challenge %q", got)
	}
}

func newServer(t *testing.T, refreshErr string) *httptest.Server {
	t.Helper()
	var challenge string
	mux := http.NewServeMux()
	var srv *httptest.Server
	mux.HandleFunc("/.well-known/oauth-authorization-server", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]string{
			"authorization_endpoint": srv.URL + "/oauth/authorize",
			"token_endpoint":         srv.URL + "/oauth/token",
			"registration_endpoint":  srv.URL + "/oauth/register",
		})
	})
	mux.HandleFunc("/oauth/register", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		uris, _ := body["redirect_uris"].([]any)
		if len(uris) != 1 || !strings.HasPrefix(uris[0].(string), "http://127.0.0.1:") {
			http.Error(w, "bad redirect", http.StatusBadRequest)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]string{"client_id": "cid"})
	})
	mux.HandleFunc("/oauth/authorize", func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		if q.Get("code_challenge_method") != "S256" || q.Get("resource") != srv.URL+"/api/v1/agent" {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		challenge = q.Get("code_challenge")
		http.Redirect(w, r, q.Get("redirect_uri")+"?code=thecode&state="+url.QueryEscape(q.Get("state")), http.StatusFound)
	})
	mux.HandleFunc("/oauth/token", func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		form, _ := url.ParseQuery(string(body))
		switch form.Get("grant_type") {
		case "authorization_code":
			if Challenge(form.Get("code_verifier")) != challenge || form.Get("code") != "thecode" {
				w.WriteHeader(http.StatusBadRequest)
				_, _ = w.Write([]byte(`{"error":"invalid_grant"}`))
				return
			}
			_, _ = w.Write([]byte(`{"access_token":"at1","refresh_token":"rt1","expires_in":3600}`))
		case "refresh_token":
			if refreshErr != "" {
				w.WriteHeader(http.StatusBadRequest)
				_, _ = w.Write([]byte(`{"error":"` + refreshErr + `"}`))
				return
			}
			_, _ = w.Write([]byte(`{"access_token":"at2","refresh_token":"rt2","expires_in":3600}`))
		}
	})
	srv = httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv
}

func TestLoginFullFlow(t *testing.T) {
	srv := newServer(t, "")
	var out strings.Builder
	var opened string
	before := time.Now()
	tok, err := Login(context.Background(), Options{
		BaseURL: srv.URL, ClientName: "Claude Code", Out: &out,
		Open: func(u string) error {
			opened = u
			go func() {
				resp, err := http.Get(u)
				if err == nil {
					resp.Body.Close()
				}
			}()
			return nil
		},
		Wait: 5 * time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}
	if tok.ClientID != "cid" || tok.AccessToken != "at1" || tok.RefreshToken != "rt1" {
		t.Fatalf("%+v", tok)
	}
	if tok.ExpiresAt.Before(before.Add(59 * time.Minute)) {
		t.Fatalf("expiry %v", tok.ExpiresAt)
	}
	if opened == "" || !strings.Contains(out.String(), opened) {
		t.Fatal("authorization URL not printed")
	}
}

func TestLoginSendsScope(t *testing.T) {
	var regScope, authScope string
	var srv *httptest.Server
	mux := http.NewServeMux()
	mux.HandleFunc("/.well-known/oauth-authorization-server", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]string{
			"authorization_endpoint": srv.URL + "/a", "token_endpoint": srv.URL + "/t",
			"registration_endpoint": srv.URL + "/r",
		})
	})
	mux.HandleFunc("/r", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		regScope, _ = body["scope"].(string)
		_ = json.NewEncoder(w).Encode(map[string]string{"client_id": "cid"})
	})
	mux.HandleFunc("/a", func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		authScope = q.Get("scope")
		http.Redirect(w, r, q.Get("redirect_uri")+"?code=c&state="+url.QueryEscape(q.Get("state")), http.StatusFound)
	})
	mux.HandleFunc("/t", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"access_token":"at","refresh_token":"rt","expires_in":60}`))
	})
	srv = httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	_, err := Login(context.Background(), Options{
		BaseURL: srv.URL, Scope: "read write", Wait: 5 * time.Second,
		Open: func(u string) error {
			go func() {
				if resp, err := http.Get(u); err == nil {
					resp.Body.Close()
				}
			}()
			return nil
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if regScope != "read write" || authScope != "read write" {
		t.Fatalf("registration %q authorization %q", regScope, authScope)
	}
}

func TestLoginTimesOut(t *testing.T) {
	srv := newServer(t, "")
	_, err := Login(context.Background(), Options{
		BaseURL: srv.URL, Open: func(string) error { return nil }, Wait: 50 * time.Millisecond,
	})
	if err == nil || !strings.Contains(err.Error(), "timed out") {
		t.Fatalf("err %v", err)
	}
}

func TestRefreshRotates(t *testing.T) {
	srv := newServer(t, "")
	tok, err := Refresh(context.Background(), nil, srv.URL, "cid", "rt1")
	if err != nil || tok.AccessToken != "at2" || tok.RefreshToken != "rt2" {
		t.Fatalf("%+v %v", tok, err)
	}
}

func TestRefreshReuseMeansLoggedOut(t *testing.T) {
	srv := newServer(t, "invalid_grant")
	if _, err := Refresh(context.Background(), nil, srv.URL, "cid", "rt1"); err != ErrLoggedOut {
		t.Fatalf("err %v", err)
	}
}

type syncBuilder struct {
	mu sync.Mutex
	b  strings.Builder
}

func (s *syncBuilder) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncBuilder) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}

// pasteLogin runs Login without a browser; feed gets the printed authorization URL and
// returns the text pasted on stdin.
func pasteLogin(t *testing.T, srv *httptest.Server, feed func(authURL string) string) (*Tokens, string, error) {
	t.Helper()
	pr, pw := io.Pipe()
	t.Cleanup(func() { pw.Close() })
	var out syncBuilder
	go func() {
		for i := 0; i < 500; i++ {
			lines := strings.Split(out.String(), "\n")
			if len(lines) > 2 && strings.HasPrefix(lines[2], "On another machine?") {
				// The other machine's browser visits the authorization URL; the redirect fails there.
				hc := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
				if resp, err := hc.Get(lines[1]); err == nil {
					resp.Body.Close()
				}
				_, _ = io.WriteString(pw, feed(lines[1]))
				return
			}
			time.Sleep(10 * time.Millisecond)
		}
	}()
	tok, err := Login(context.Background(), Options{
		BaseURL: srv.URL, Out: &out, NoBrowser: true, Paste: pr, Wait: 2 * time.Second,
		Open: func(string) error { t.Error("browser opened"); return nil },
	})
	return tok, out.String(), err
}

func callbackFor(authURL, code, state string) string {
	u, _ := url.Parse(authURL)
	q := u.Query()
	if state == "" {
		state = q.Get("state")
	}
	return q.Get("redirect_uri") + "?code=" + code + "&state=" + url.QueryEscape(state) + "\n"
}

func TestLoginPaste(t *testing.T) {
	srv := newServer(t, "")
	tok, out, err := pasteLogin(t, srv, func(a string) string { return callbackFor(a, "thecode", "") })
	if err != nil {
		t.Fatal(err)
	}
	if tok.AccessToken != "at1" || !strings.Contains(out, "paste the address your browser could not open here") {
		t.Fatalf("%+v %q", tok, out)
	}
}

func TestLoginPasteGarbageAndWrongStateKeepWaiting(t *testing.T) {
	srv := newServer(t, "")
	tok, out, err := pasteLogin(t, srv, func(a string) string {
		return "garbage\n" + callbackFor(a, "evil", "other") + callbackFor(a, "thecode", "")
	})
	if err != nil {
		t.Fatal(err)
	}
	if tok.AccessToken != "at1" || !strings.Contains(out, "not the callback address") ||
		!strings.Contains(out, "state mismatch") {
		t.Fatalf("%+v %q", tok, out)
	}
}

func TestLoginPasteWrongStateIsRefused(t *testing.T) {
	srv := newServer(t, "")
	_, out, err := pasteLogin(t, srv, func(a string) string { return callbackFor(a, "thecode", "other") })
	if err == nil || !strings.Contains(err.Error(), "timed out") || !strings.Contains(out, "state mismatch") {
		t.Fatalf("%v %q", err, out)
	}
}
