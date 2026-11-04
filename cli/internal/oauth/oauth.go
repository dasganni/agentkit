// Package oauth implements the browser login (discovery, dynamic client registration,
// PKCE S256, loopback callback) and the refresh-token grant.
package oauth

import (
	"bufio"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os/exec"
	"runtime"
	"strings"
	"time"
)

// ErrLoggedOut means the refresh token was rejected; the user must log in again.
var ErrLoggedOut = errors.New("logged out: the refresh token is no longer valid, run login again")

// Tokens is the result of a code exchange or refresh.
type Tokens struct {
	ClientID     string
	AccessToken  string
	RefreshToken string
	ExpiresAt    time.Time
}

// Options configure Login.
type Options struct {
	BaseURL    string
	ClientName string
	// Scope is the space-separated OAuth scope; empty sends none.
	Scope string
	HTTP       *http.Client
	Out        io.Writer
	// Open starts the browser; nil uses the platform opener. The URL is always printed.
	Open func(url string) error
	// NoBrowser prints the URL without trying to open a browser.
	NoBrowser bool
	// Paste, when set, is read line by line for the callback URL a browser on another
	// machine failed to open; the loopback callback and a pasted URL race.
	Paste io.Reader
	// Wait bounds the wait for the callback; zero means five minutes.
	Wait time.Duration
}

type metadata struct {
	AuthorizationEndpoint string `json:"authorization_endpoint"`
	TokenEndpoint         string `json:"token_endpoint"`
	RegistrationEndpoint  string `json:"registration_endpoint"`
}

type tokenResponse struct {
	AccessToken  string `json:"access_token"`
	RefreshToken string `json:"refresh_token"`
	ExpiresIn    int    `json:"expires_in"`
	Error        string `json:"error"`
}

// Resource is the OAuth resource indicator for the agent API.
func Resource(baseURL string) string { return strings.TrimRight(baseURL, "/") + "/api/v1/agent" }

// Challenge is the S256 code challenge of a verifier.
func Challenge(verifier string) string {
	sum := sha256.Sum256([]byte(verifier))
	return base64.RawURLEncoding.EncodeToString(sum[:])
}

func randomString(n int) (string, error) {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}

func discover(ctx context.Context, hc *http.Client, baseURL string) (*metadata, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		strings.TrimRight(baseURL, "/")+"/.well-known/oauth-authorization-server", nil)
	if err != nil {
		return nil, err
	}
	resp, err := hc.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("discovery failed: HTTP %d", resp.StatusCode)
	}
	m := &metadata{}
	if err := json.NewDecoder(resp.Body).Decode(m); err != nil {
		return nil, fmt.Errorf("discovery: %w", err)
	}
	if m.AuthorizationEndpoint == "" || m.TokenEndpoint == "" {
		return nil, errors.New("discovery: authorization or token endpoint missing")
	}
	return m, nil
}

func register(ctx context.Context, hc *http.Client, m *metadata, name, redirect, scope string) (string, error) {
	if m.RegistrationEndpoint == "" {
		return "", errors.New("the server offers no client registration")
	}
	meta := map[string]any{
		"client_name":                name,
		"redirect_uris":              []string{redirect},
		"grant_types":                []string{"authorization_code", "refresh_token"},
		"response_types":             []string{"code"},
		"token_endpoint_auth_method": "none",
	}
	if scope != "" {
		meta["scope"] = scope
	}
	body, _ := json.Marshal(meta)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, m.RegistrationEndpoint, strings.NewReader(string(body)))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := hc.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	var out struct {
		ClientID string `json:"client_id"`
	}
	if resp.StatusCode >= 300 || json.NewDecoder(resp.Body).Decode(&out) != nil || out.ClientID == "" {
		return "", fmt.Errorf("client registration failed: HTTP %d", resp.StatusCode)
	}
	return out.ClientID, nil
}

func tokenRequest(ctx context.Context, hc *http.Client, endpoint string, form url.Values) (*tokenResponse, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, strings.NewReader(form.Encode()))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := hc.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	tr := &tokenResponse{}
	_ = json.NewDecoder(resp.Body).Decode(tr)
	if resp.StatusCode == http.StatusBadRequest || resp.StatusCode == http.StatusUnauthorized {
		if tr.Error == "invalid_grant" || tr.Error == "" {
			return nil, ErrLoggedOut
		}
		return nil, fmt.Errorf("token request rejected: %s", tr.Error)
	}
	if resp.StatusCode != http.StatusOK || tr.AccessToken == "" {
		return nil, fmt.Errorf("token request failed: HTTP %d", resp.StatusCode)
	}
	return tr, nil
}

func toTokens(clientID string, tr *tokenResponse, now time.Time, oldRefresh string) *Tokens {
	t := &Tokens{ClientID: clientID, AccessToken: tr.AccessToken, RefreshToken: tr.RefreshToken}
	if t.RefreshToken == "" {
		t.RefreshToken = oldRefresh
	}
	t.ExpiresAt = now.Add(time.Duration(tr.ExpiresIn) * time.Second)
	return t
}

// Refresh exchanges the refresh token; a rejected token returns ErrLoggedOut.
func Refresh(ctx context.Context, hc *http.Client, baseURL, clientID, refreshToken string) (*Tokens, error) {
	if hc == nil {
		hc = http.DefaultClient
	}
	m, err := discover(ctx, hc, baseURL)
	if err != nil {
		return nil, err
	}
	now := time.Now()
	tr, err := tokenRequest(ctx, hc, m.TokenEndpoint, url.Values{
		"grant_type":    {"refresh_token"},
		"refresh_token": {refreshToken},
		"client_id":     {clientID},
		"resource":      {Resource(baseURL)},
	})
	if err != nil {
		return nil, err
	}
	return toTokens(clientID, tr, now, refreshToken), nil
}

type callback struct{ code, errText string }

// Login runs the browser flow and returns the tokens.
func Login(ctx context.Context, o Options) (*Tokens, error) {
	hc := o.HTTP
	if hc == nil {
		hc = http.DefaultClient
	}
	wait := o.Wait
	if wait == 0 {
		wait = 5 * time.Minute
	}
	m, err := discover(ctx, hc, o.BaseURL)
	if err != nil {
		return nil, err
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return nil, err
	}
	defer ln.Close()
	redirect := fmt.Sprintf("http://%s/callback", ln.Addr().String())
	clientID, err := register(ctx, hc, m, o.ClientName, redirect, o.Scope)
	if err != nil {
		return nil, err
	}
	verifier, err := randomString(48)
	if err != nil {
		return nil, err
	}
	state, err := randomString(24)
	if err != nil {
		return nil, err
	}
	authURL, err := url.Parse(m.AuthorizationEndpoint)
	if err != nil {
		return nil, err
	}
	q := authURL.Query()
	for k, v := range map[string]string{
		"response_type": "code", "client_id": clientID, "redirect_uri": redirect,
		"code_challenge": Challenge(verifier), "code_challenge_method": "S256",
		"state": state, "resource": Resource(o.BaseURL),
	} {
		q.Set(k, v)
	}
	if o.Scope != "" {
		q.Set("scope", o.Scope)
	}
	authURL.RawQuery = q.Encode()

	results := make(chan callback, 1)
	srv := &http.Server{Handler: callbackHandler(state, results), ReadHeaderTimeout: 10 * time.Second}
	go func() { _ = srv.Serve(ln) }()
	defer srv.Close()

	out := o.Out
	if out == nil {
		out = io.Discard
	}
	fmt.Fprintf(out, "Open this URL to sign in:\n%s\n", authURL.String())
	fmt.Fprintln(out, "On another machine? Open the URL there, then paste the address your browser could not open here.")
	if !o.NoBrowser {
		open := o.Open
		if open == nil {
			open = openBrowser
		}
		if err := open(authURL.String()); err != nil {
			fmt.Fprintf(out, "Could not open a browser (%v); open the URL manually.\n", err)
		}
	}
	if o.Paste != nil {
		go readPasted(o.Paste, state, results, out)
	}

	select {
	case res := <-results:
		if res.errText != "" {
			return nil, fmt.Errorf("login denied: %s", res.errText)
		}
		now := time.Now()
		tr, err := tokenRequest(ctx, hc, m.TokenEndpoint, url.Values{
			"grant_type":    {"authorization_code"},
			"code":          {res.code},
			"redirect_uri":  {redirect},
			"client_id":     {clientID},
			"code_verifier": {verifier},
			"resource":      {Resource(o.BaseURL)},
		})
		if err != nil {
			return nil, err
		}
		return toTokens(clientID, tr, now, ""), nil
	case <-time.After(wait):
		return nil, fmt.Errorf("login timed out after %s", wait)
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

// evaluate checks the state of a callback query and extracts the code or error.
func evaluate(q url.Values, state string) (callback, error) {
	switch {
	case q.Get("state") != state:
		return callback{}, errors.New("state mismatch")
	case q.Get("error") != "":
		return callback{errText: q.Get("error")}, nil
	}
	return callback{code: q.Get("code")}, nil
}

// readPasted reads pasted callback URLs until one is accepted; bad input is reported
// and the wait goes on.
func readPasted(r io.Reader, state string, results chan<- callback, out io.Writer) {
	sc := bufio.NewScanner(r)
	sc.Buffer(nil, 1<<20)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" {
			continue
		}
		u, err := url.Parse(line)
		if err != nil || u.Scheme == "" || u.Host == "" || u.Query().Get("state") == "" {
			fmt.Fprintln(out, "That is not the callback address; paste the full URL (http://127.0.0.1:<port>/callback?code=...&state=...).")
			continue
		}
		res, err := evaluate(u.Query(), state)
		if err != nil {
			fmt.Fprintf(out, "Pasted address refused: %v. Paste the URL from this login attempt.\n", err)
			continue
		}
		if res.errText == "" && res.code == "" {
			fmt.Fprintln(out, "The pasted address has no code; paste the full URL.")
			continue
		}
		select {
		case results <- res:
		default:
		}
		return
	}
}

func callbackHandler(state string, results chan<- callback) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/callback", func(w http.ResponseWriter, r *http.Request) {
		res, err := evaluate(r.URL.Query(), state)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		fmt.Fprintln(w, "You can close this window and return to the terminal.")
		select {
		case results <- res:
		default:
		}
	})
	return mux
}

func openBrowser(u string) error {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		cmd = exec.Command("open", u)
	case "windows":
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", u)
	default:
		cmd = exec.Command("xdg-open", u)
	}
	return cmd.Start()
}
