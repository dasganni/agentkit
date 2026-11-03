package tools

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

const schema = `[{"name":"notes","description":"Notes.","ops":["list","add"],"params":{"type":"object",
"properties":{"limit":{"type":"integer"},"ratio":{"type":"number"},"text":{"type":"string"},
"pinned":{"type":"boolean"},"kind":{"type":"string","enum":["todo","idea"]},
"meta":{"type":"object"},"tags":{"type":"array"}},"required":["text"]}},
{"name":"ping","description":"Ping.","params":{"type":"object","properties":{}}}]`

func load(t *testing.T) []Tool {
	t.Helper()
	list, err := Load([]byte(schema))
	if err != nil {
		t.Fatal(err)
	}
	return list
}

func TestHelpIsOffline(t *testing.T) {
	list := load(t)
	h := Find(list, "notes").Help("demo")
	for _, want := range []string{"Usage: demo notes <op>", "Ops: list, add", "--limit int", "--pinned bool",
		"--meta json", "(one of: todo, idea)", "(required)"} {
		if !strings.Contains(h, want) {
			t.Errorf("help lacks %q:\n%s", want, h)
		}
	}
}

func TestFlagTypes(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "meta.json")
	if err := os.WriteFile(file, []byte(`{"a":1}`), 0o600); err != nil {
		t.Fatal(err)
	}
	notes := Find(load(t), "notes")
	got, err := notes.Parse([]string{"add", "--text", "hi", "--limit", "3", "--ratio=1.5", "--pinned",
		"--kind", "idea", "--meta", "@" + file, "--tags", `["x","y"]`})
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]any{"op": "add", "text": "hi", "limit": int64(3), "ratio": 1.5, "pinned": true,
		"kind": "idea", "meta": map[string]any{"a": float64(1)}, "tags": []any{"x", "y"}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v", got)
	}
	got, err = notes.Parse([]string{"list", "--text=t", "--pinned=false"})
	if err != nil || got["pinned"] != false {
		t.Fatalf("got %#v, %v", got, err)
	}
}

func TestUsageErrors(t *testing.T) {
	notes := Find(load(t), "notes")
	for _, args := range [][]string{
		{"add", "--text", "x", "--nope"},
		{"add", "--text", "x", "--limit", "abc"},
		{"add", "--text", "x", "--kind", "bad"},
		{"add", "--text", "x", "--meta", "{"},
		{"add"},
		{"bogus", "--text", "x"},
		{"--text", "x"},
		{"add", "--text", "x", "extra"},
	} {
		_, err := notes.Parse(args)
		var ue *UsageError
		if !errors.As(err, &ue) {
			t.Errorf("%v: want usage error, got %v", args, err)
		}
	}
}

func TestHelpFlag(t *testing.T) {
	list := load(t)
	for _, args := range [][]string{{"--help"}, {"add", "-h"}} {
		if _, err := Find(list, "notes").Parse(args); !errors.Is(err, ErrHelp) {
			t.Errorf("%v: got %v", args, err)
		}
	}
	if _, err := Find(list, "ping").Parse([]string{"--help"}); !errors.Is(err, ErrHelp) {
		t.Error("ping help")
	}
}

func TestNoOps(t *testing.T) {
	got, err := Find(load(t), "ping").Parse(nil)
	if err != nil || len(got) != 0 {
		t.Fatalf("%v %v", got, err)
	}
}
