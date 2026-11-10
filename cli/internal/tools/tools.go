// Package tools turns the embedded tool schema into command-line parsing and help:
// `<app> <tool> [<op>] --<field>`, with flag types taken from the JSON schema.
package tools

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"slices"
	"sort"
	"strconv"
	"strings"
)

// UsageError is a command-line mistake; the CLI exits with 2.
type UsageError struct{ Msg string }

func (e *UsageError) Error() string { return e.Msg }

// ErrHelp is returned by Parse when help was requested.
var ErrHelp = errors.New("help requested")

// Prop is one field of a tool's parameter schema.
type Prop struct {
	Type        string `json:"type"`
	Description string `json:"description"`
	Enum        []any  `json:"enum"`
	Encoding    string `json:"contentEncoding"`
}

// Params is the object schema of a tool's parameters.
type Params struct {
	Properties map[string]Prop `json:"properties"`
	Required   []string        `json:"required"`
}

// Tool is one entry of the schema list.
type Tool struct {
	Name        string   `json:"name"`
	Description string   `json:"description"`
	Ops         []string `json:"ops"`
	Params      Params   `json:"params"`
	Write       bool     `json:"write"`
}

// Load decodes a schema list.
func Load(data []byte) ([]Tool, error) {
	var list []Tool
	if err := json.Unmarshal(data, &list); err != nil {
		return nil, fmt.Errorf("tool schema: %w", err)
	}
	return list, nil
}

// Find returns the tool with the given name.
func Find(list []Tool, name string) *Tool {
	for i := range list {
		if list[i].Name == name {
			return &list[i]
		}
	}
	return nil
}

func (t *Tool) fieldNames() []string {
	names := make([]string, 0, len(t.Params.Properties))
	for n := range t.Params.Properties {
		names = append(names, n)
	}
	sort.Strings(names)
	return names
}

func (p Prop) isBase64() bool { return p.Type == "string" && p.Encoding == "base64" }

func typeLabel(p Prop) string {
	if p.isBase64() {
		return "base64|@file"
	}
	switch p.Type {
	case "integer":
		return "int"
	case "number":
		return "number"
	case "boolean":
		return "bool"
	case "object", "array":
		return "json"
	}
	return "string"
}

// Help is the offline usage text of the tool.
func (t *Tool) Help(app string) string {
	var b strings.Builder
	usage := app + " " + t.Name
	if len(t.Ops) > 0 {
		usage += " <op>"
	}
	fmt.Fprintf(&b, "Usage: %s [flags]\n", usage)
	if t.Description != "" {
		fmt.Fprintf(&b, "\n%s\n", t.Description)
	}
	if len(t.Ops) > 0 {
		fmt.Fprintf(&b, "\nOps: %s\n", strings.Join(t.Ops, ", "))
	}
	b.WriteString("\nFlags:\n")
	for _, n := range t.fieldNames() {
		p := t.Params.Properties[n]
		line := fmt.Sprintf("  --%s %s", n, typeLabel(p))
		if p.Description != "" {
			line += "  " + p.Description
		}
		if len(p.Enum) > 0 {
			vals := make([]string, len(p.Enum))
			for i, e := range p.Enum {
				vals[i] = fmt.Sprint(e)
			}
			line += " (one of: " + strings.Join(vals, ", ") + ")"
		}
		if slices.Contains(t.Params.Required, n) {
			line += " (required)"
		}
		b.WriteString(line + "\n")
	}
	b.WriteString("  --json  print the raw JSON answer\n")
	return b.String()
}

func parseValue(name string, p Prop, raw string) (any, error) {
	var v any
	switch p.Type {
	case "integer":
		n, err := strconv.ParseInt(raw, 10, 64)
		if err != nil {
			return nil, fmt.Errorf("%q is not an integer", raw)
		}
		v = n
	case "number":
		n, err := strconv.ParseFloat(raw, 64)
		if err != nil {
			return nil, fmt.Errorf("%q is not a number", raw)
		}
		v = n
	case "boolean":
		b, err := strconv.ParseBool(raw)
		if err != nil {
			return nil, fmt.Errorf("%q is not a boolean", raw)
		}
		v = b
	case "object", "array":
		data := []byte(raw)
		if rest, ok := strings.CutPrefix(raw, "@"); ok {
			var err error
			if data, err = os.ReadFile(rest); err != nil {
				return nil, err
			}
		}
		if err := json.Unmarshal(data, &v); err != nil {
			return nil, fmt.Errorf("invalid JSON: %w", err)
		}
	default:
		v = raw
		if path, ok := strings.CutPrefix(raw, "@"); ok && p.isBase64() {
			data, err := os.ReadFile(path)
			if err != nil {
				return nil, fmt.Errorf("--%s: cannot read %s: %w", name, path, err)
			}
			v = base64.StdEncoding.EncodeToString(data)
		}
	}
	if len(p.Enum) > 0 {
		ok := slices.ContainsFunc(p.Enum, func(e any) bool { return fmt.Sprint(e) == fmt.Sprint(v) })
		if !ok {
			return nil, fmt.Errorf("%v is not one of %v", v, p.Enum)
		}
	}
	return v, nil
}

// Parse reads `[<op>] --<field> value ...` into the request params; the op, when the
// tool has ops, is sent as params["op"]. ErrHelp means help was requested.
func (t *Tool) Parse(args []string) (map[string]any, error) {
	params := map[string]any{}
	if len(t.Ops) > 0 && len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		if !slices.Contains(t.Ops, args[0]) {
			return nil, &UsageError{fmt.Sprintf("unknown op %q, expected one of: %s", args[0], strings.Join(t.Ops, ", "))}
		}
		params["op"] = args[0]
		args = args[1:]
	} else if len(t.Ops) > 0 && !slices.Contains(args, "-h") && !slices.Contains(args, "--help") {
		return nil, &UsageError{"missing op, expected one of: " + strings.Join(t.Ops, ", ")}
	}

	fs := flag.NewFlagSet(t.Name, flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	fs.Usage = func() {}
	for _, n := range t.fieldNames() {
		p := t.Params.Properties[n]
		set := func(raw string) error {
			v, err := parseValue(n, p, raw)
			if err != nil {
				return err
			}
			params[n] = v
			return nil
		}
		if p.Type == "boolean" {
			fs.BoolFunc(n, p.Description, set)
		} else {
			fs.Func(n, p.Description, set)
		}
	}
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil, ErrHelp
		}
		return nil, &UsageError{err.Error()}
	}
	if fs.NArg() > 0 {
		return nil, &UsageError{"unexpected argument " + strconv.Quote(fs.Arg(0))}
	}
	for _, r := range t.Params.Required {
		if _, ok := params[r]; !ok {
			return nil, &UsageError{"missing required flag --" + r}
		}
	}
	return params, nil
}
