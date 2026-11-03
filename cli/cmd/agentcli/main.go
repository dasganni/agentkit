// Command agentcli is the generic agent CLI; the app name, environment prefix and
// default URL are set at build time and the tool schema is embedded.
package main

import (
	"context"
	_ "embed"
	"fmt"
	"os"
	"os/signal"
	"strings"

	"github.com/dasganni/agentkit/cli/internal/client"
	"github.com/dasganni/agentkit/cli/internal/cmd"
	"github.com/dasganni/agentkit/cli/internal/config"
	"github.com/dasganni/agentkit/cli/internal/oauth"
	"github.com/dasganni/agentkit/cli/internal/tools"
)

// Set with -ldflags "-X main.app=... -X main.envPrefix=... -X main.defaultURL=...".
var (
	app        = "agentcli"
	envPrefix  = ""
	defaultURL = ""
)

//go:embed tools.json
var schema []byte

// bridgeEnv copies <envPrefix>_URL and _TOKEN to the names the config store reads
// (derived from the app name) when a distinct prefix was built in.
func bridgeEnv() {
	if envPrefix == "" {
		return
	}
	target := strings.ToUpper(strings.NewReplacer("-", "_", ".", "_").Replace(app))
	for _, suffix := range []string{"URL", "TOKEN"} {
		if v := os.Getenv(envPrefix + "_" + suffix); v != "" {
			os.Setenv(target+"_"+suffix, v)
		}
	}
}

func main() { os.Exit(run()) }

func run() int {
	bridgeEnv()
	list, err := tools.Load(schema)
	if err != nil {
		fmt.Fprintf(os.Stderr, "%s: %v\n", app, err)
		return 1
	}
	store, err := config.NewStore(app, defaultURL)
	if err != nil {
		fmt.Fprintf(os.Stderr, "%s: %v\n", app, err)
		return 1
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	a := &cmd.App{
		Name: app, Tools: list, Store: store, Client: &client.Client{Store: store},
		Login: oauth.Login, In: os.Stdin, Out: os.Stdout, Err: os.Stderr,
	}
	return a.Run(ctx, os.Args[1:])
}
