// Package cmd runs the CLI: account commands, `tools`, and the commands generated from
// the tool schema. Exit codes: 0 ok, 1 error, 2 usage, 3 not logged in or token rejected.
package cmd

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/dasganni/agentkit/cli/internal/client"
	"github.com/dasganni/agentkit/cli/internal/config"
	"github.com/dasganni/agentkit/cli/internal/oauth"
	"github.com/dasganni/agentkit/cli/internal/tools"
	"github.com/dasganni/agentkit/cli/internal/version"
)

const (
	exitError = 1
	exitUsage = 2
	exitAuth  = 3

	toolsPath = "/api/v1/agent/tools"
	tokenPath = "/api/v1/agent/token"
)

// App is one built CLI: its name, tool schema, storage and streams.
type App struct {
	Name   string
	Tools  []tools.Tool
	Store  *config.Store
	Client *client.Client
	Login  func(ctx context.Context, o oauth.Options) (*oauth.Tokens, error)
	In     io.Reader
	Out    io.Writer
	Err    io.Writer
}

type answer struct {
	Text   string           `json:"text"`
	Images []map[string]any `json:"images"`
}

type apiError struct {
	Error string `json:"error"`
	Field string `json:"field"`
}

func (a *App) usage() string {
	var b strings.Builder
	fmt.Fprintf(&b, "Usage: %s [--json] <command> [flags]\n\nAccount commands:\n", a.Name)
	b.WriteString("  login [--url URL] [--token-stdin]   sign in\n")
	b.WriteString("  logout                              revoke the grant and forget the login\n")
	b.WriteString("  status                              show the login and check it\n")
	b.WriteString("  token revoke                        revoke the grant on the server\n")
	b.WriteString("  tools                               list the tools\n")
	b.WriteString("  version                             print the version\n")
	if len(a.Tools) > 0 {
		fmt.Fprintf(&b, "\nTools (%s <tool> --help for flags):\n", a.Name)
		for _, t := range a.Tools {
			fmt.Fprintf(&b, "  %-12s %s\n", t.Name, t.Description)
		}
	}
	return b.String()
}

// Run executes one command line and returns the process exit code.
func (a *App) Run(ctx context.Context, args []string) int {
	raw := false
	rest := make([]string, 0, len(args))
	for _, s := range args {
		if s == "--json" {
			raw = true
			continue
		}
		rest = append(rest, s)
	}
	err := a.dispatch(ctx, rest, raw)
	if err == nil || errors.Is(err, tools.ErrHelp) {
		return 0
	}
	fmt.Fprintf(a.Err, "%s: %v\n", a.Name, err)
	var ue *tools.UsageError
	var ae *client.AuthError
	switch {
	case errors.As(err, &ue):
		return exitUsage
	case errors.As(err, &ae):
		fmt.Fprintf(a.Err, "run %s login to sign in\n", a.Name)
		return exitAuth
	}
	return exitError
}

func usageErr(format string, v ...any) error {
	return &tools.UsageError{Msg: fmt.Sprintf(format, v...)}
}

func (a *App) dispatch(ctx context.Context, args []string, raw bool) error {
	if len(args) == 0 {
		return usageErr("missing command\n\n%s", a.usage())
	}
	name, rest := args[0], args[1:]
	switch name {
	case "-h", "--help", "help":
		fmt.Fprint(a.Out, a.usage())
		return nil
	case "version":
		fmt.Fprintf(a.Out, "%s %s (agent protocol %d)\n", a.Name, version.Version, version.Protocol)
		return nil
	case "tools":
		return a.listTools(raw)
	case "login":
		return a.login(ctx, rest)
	case "logout":
		return a.revoke(ctx, "logged out")
	case "status":
		return a.status(ctx)
	case "token":
		if len(rest) == 1 && rest[0] == "revoke" {
			return a.revoke(ctx, "token revoked")
		}
		return usageErr("usage: %s token revoke", a.Name)
	}
	tool := tools.Find(a.Tools, name)
	if tool == nil {
		return usageErr("unknown command %q\n\n%s", name, a.usage())
	}
	return a.runTool(ctx, tool, rest, raw)
}

func (a *App) listTools(raw bool) error {
	if raw {
		return json.NewEncoder(a.Out).Encode(a.Tools)
	}
	for _, t := range a.Tools {
		mode := "read"
		if t.Write {
			mode = "write"
		}
		fmt.Fprintf(a.Out, "%s\t%s\t%s\n", t.Name, mode, t.Description)
	}
	return nil
}

func (a *App) runTool(ctx context.Context, tool *tools.Tool, args []string, raw bool) error {
	params, err := tool.Parse(args)
	if errors.Is(err, tools.ErrHelp) {
		fmt.Fprint(a.Out, tool.Help(a.Name))
		return err
	}
	if err != nil {
		return err
	}
	resp, err := a.Client.Do(ctx, http.MethodPost, toolsPath+"/"+tool.Name, params)
	if err != nil {
		return err
	}
	if resp.Status/100 != 2 {
		return responseError(resp)
	}
	if raw {
		fmt.Fprintln(a.Out, strings.TrimRight(string(resp.Body), "\n"))
		return nil
	}
	var ans answer
	if err := json.Unmarshal(resp.Body, &ans); err != nil {
		return fmt.Errorf("unreadable answer: %w", err)
	}
	fmt.Fprintln(a.Out, ans.Text)
	if n := len(ans.Images); n > 0 {
		fmt.Fprintf(a.Err, "%d image(s) not shown, use --json\n", n)
	}
	return nil
}

func responseError(resp *client.Response) error {
	var e apiError
	if json.Unmarshal(resp.Body, &e) == nil && e.Error != "" {
		if e.Field != "" {
			return fmt.Errorf("%s (field %s)", e.Error, e.Field)
		}
		return errors.New(e.Error)
	}
	return fmt.Errorf("server answered HTTP %d", resp.Status)
}

func (a *App) login(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("login", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	urlFlag := fs.String("url", "", "server URL")
	stdin := fs.Bool("token-stdin", false, "read the token from stdin")
	if err := fs.Parse(args); err != nil {
		return usageErr("login: %v", err)
	}
	if fs.NArg() > 0 {
		return usageErr("login: unexpected argument %q", fs.Arg(0))
	}
	cfg, err := a.Store.Load()
	if err != nil {
		return err
	}
	base := cfg.BaseURL()
	if *urlFlag != "" {
		cfg.URL = strings.TrimRight(*urlFlag, "/")
		base = cfg.URL
	}
	if base == "" {
		return usageErr("no server URL: pass --url")
	}
	if *stdin {
		line, err := bufio.NewReader(a.In).ReadString('\n')
		token := strings.TrimSpace(line)
		if token == "" {
			if err != nil && !errors.Is(err, io.EOF) {
				return err
			}
			return usageErr("no token on stdin")
		}
		cfg.AccessToken, cfg.RefreshToken, cfg.ClientID, cfg.ExpiresAt = token, "", "", time.Time{}
	} else {
		tok, err := a.Login(ctx, oauth.Options{BaseURL: base, ClientName: a.Name, Out: a.Out})
		if err != nil {
			return err
		}
		cfg.ClientID, cfg.AccessToken, cfg.RefreshToken, cfg.ExpiresAt =
			tok.ClientID, tok.AccessToken, tok.RefreshToken, tok.ExpiresAt
	}
	if err := a.Store.Save(cfg); err != nil {
		return err
	}
	fmt.Fprintf(a.Out, "logged in to %s\n", base)
	return nil
}

// revoke asks the server to revoke the calling grant, then deletes the local login.
// A token the server already rejects counts as revoked.
func (a *App) revoke(ctx context.Context, done string) error {
	cfg, err := a.Store.Load()
	if err != nil {
		return err
	}
	if cfg.Token() != "" {
		resp, err := a.Client.Do(ctx, http.MethodDelete, tokenPath, nil)
		var ae *client.AuthError
		switch {
		case errors.As(err, &ae):
		case err != nil:
			return fmt.Errorf("revoke on the server failed, login kept: %w", err)
		case resp.Status/100 != 2:
			return fmt.Errorf("revoke on the server failed, login kept: %w", responseError(resp))
		}
	}
	if err := a.Store.Clear(); err != nil {
		return err
	}
	fmt.Fprintln(a.Out, done)
	return nil
}

func (a *App) status(ctx context.Context) error {
	cfg, err := a.Store.Load()
	if err != nil {
		return err
	}
	fmt.Fprintf(a.Out, "server: %s\n", cfg.BaseURL())
	if cfg.Token() == "" {
		return &client.AuthError{Msg: "not logged in"}
	}
	source := "stored login"
	if cfg.EnvToken() {
		source = "environment token"
	} else if !cfg.ExpiresAt.IsZero() {
		source += ", access token expires " + cfg.ExpiresAt.Format(time.RFC3339)
	}
	fmt.Fprintf(a.Out, "auth: %s\n", source)
	resp, err := a.Client.Do(ctx, http.MethodGet, toolsPath, nil)
	if err != nil {
		return err
	}
	if resp.Status/100 != 2 {
		return responseError(resp)
	}
	fmt.Fprintln(a.Out, "state: ok")
	return nil
}
