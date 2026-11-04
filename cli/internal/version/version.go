// Package version holds the CLI release and the agent protocol version.
package version

// Version is the release; cli-dist.sh overrides it with -ldflags -X.
var Version = "0.1.2"

const Protocol = 1
