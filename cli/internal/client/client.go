// Package client calls the agent API with the stored login: Bearer token, protocol
// header, renewal one minute before expiry and once after a 401, under a lock file.
package client

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"time"

	"github.com/dasganni/agentkit/cli/internal/config"
	"github.com/dasganni/agentkit/cli/internal/oauth"
	"github.com/dasganni/agentkit/cli/internal/version"
)

const (
	renewMargin = time.Minute
	// lockHold bounds what a renewal does under the lock, below config.LockStale, so a live holder is never
	// taken over; lockWait exceeds config.LockStale, so a waiter outlasts a crashed holder's lock.
	lockWait = time.Minute + 15*time.Second
)

var lockHold = 20 * time.Second

// AuthError means the login is missing or no longer accepted; the CLI exits with 3.
type AuthError struct{ Msg string }

func (e *AuthError) Error() string { return e.Msg }

// ExitCode is the process exit code for this error.
func (e *AuthError) ExitCode() int { return 3 }

// ProtocolError means the server requires a different agent protocol (HTTP 426).
type ProtocolError struct{ Msg string }

func (e *ProtocolError) Error() string { return e.Msg }

// Response is a completed HTTP exchange.
type Response struct {
	Status int
	Body   []byte
}

// Client talks to the agent API of one app.
type Client struct {
	Store *config.Store
	HTTP  *http.Client
	Now   func() time.Time
}

func (c *Client) now() time.Time {
	if c.Now != nil {
		return c.Now()
	}
	return time.Now()
}

func (c *Client) httpClient() *http.Client {
	if c.HTTP != nil {
		return c.HTTP
	}
	return http.DefaultClient
}

func (c *Client) expiring(cfg *config.Config) bool {
	return !cfg.ExpiresAt.IsZero() && !c.now().Add(renewMargin).Before(cfg.ExpiresAt)
}

// Do sends one request (body is JSON-encoded when not nil) to baseURL+path.
func (c *Client) Do(ctx context.Context, method, path string, body any) (*Response, error) {
	cfg, err := c.Store.Load()
	if err != nil {
		return nil, err
	}
	token := cfg.Token()
	if token == "" {
		return nil, &AuthError{"not logged in, run login first"}
	}
	if !cfg.EnvToken() && cfg.RefreshToken != "" && c.expiring(cfg) {
		if token, err = c.renew(ctx, token); err != nil {
			return nil, err
		}
	}
	resp, err := c.send(ctx, cfg.BaseURL(), method, path, token, body)
	if err != nil {
		return nil, err
	}
	if resp.Status != http.StatusUnauthorized {
		return resp, nil
	}
	if cfg.EnvToken() || cfg.RefreshToken == "" {
		return nil, &AuthError{"the token was rejected, run login again"}
	}
	if token, err = c.renew(ctx, token); err != nil {
		return nil, err
	}
	if resp, err = c.send(ctx, cfg.BaseURL(), method, path, token, body); err != nil {
		return nil, err
	}
	if resp.Status == http.StatusUnauthorized {
		return nil, &AuthError{"the token was rejected after renewal, run login again"}
	}
	return resp, nil
}

func (c *Client) send(ctx context.Context, baseURL, method, path, token string, body any) (*Response, error) {
	var rd io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		rd = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(ctx, method, baseURL+path, rd)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Agent-Protocol", strconv.Itoa(version.Protocol))
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.httpClient().Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode == http.StatusUpgradeRequired {
		msg := fmt.Sprintf("this CLI speaks agent protocol %d, which the server does not accept", version.Protocol)
		var e struct {
			Error string `json:"error"`
		}
		if json.Unmarshal(data, &e) == nil && e.Error != "" {
			msg += ": " + e.Error
		}
		return nil, &ProtocolError{msg + "; update the CLI or the server, whichever is older"}
	}
	return &Response{Status: resp.StatusCode, Body: data}, nil
}

// renew returns a fresh access token. staleToken is the token the caller just used:
// if another process already replaced it, that newer token is returned unchanged.
func (c *Client) renew(ctx context.Context, staleToken string) (string, error) {
	release, err := c.Store.Lock(lockWait)
	if err != nil {
		return "", err
	}
	defer release()
	cfg, err := c.Store.Load()
	if err != nil {
		return "", err
	}
	if cfg.AccessToken != staleToken && cfg.AccessToken != "" && !c.expiring(cfg) {
		return cfg.AccessToken, nil
	}
	if cfg.RefreshToken == "" {
		return "", &AuthError{"not logged in, run login first"}
	}
	rctx, cancel := context.WithTimeout(ctx, lockHold)
	defer cancel()
	t, err := oauth.Refresh(rctx, c.httpClient(), cfg.BaseURL(), cfg.ClientID, cfg.RefreshToken)
	if errors.Is(err, oauth.ErrLoggedOut) {
		cfg.AccessToken, cfg.RefreshToken, cfg.ExpiresAt = "", "", time.Time{}
		if serr := c.Store.Save(cfg); serr != nil {
			return "", serr
		}
		return "", &AuthError{oauth.ErrLoggedOut.Error()}
	}
	if err != nil {
		return "", err
	}
	cfg.AccessToken, cfg.RefreshToken, cfg.ExpiresAt = t.AccessToken, t.RefreshToken, t.ExpiresAt
	if err := c.Store.Save(cfg); err != nil {
		return "", err
	}
	return t.AccessToken, nil
}
