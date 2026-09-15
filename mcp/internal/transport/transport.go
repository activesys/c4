// Package transport provides the dual-mode MCP serving entry point shared by
// all C4 MCP services:
//
//   - Resident mode (default, production shape — c4_architecture.md §3.1.1 /
//     c4_deployment.md §6.5): the process listens on the Unix socket
//     <sock-dir>/<service>.sock and serves one MCP session per connection.
//     A resident process starts with zero data-path instances and zero shm
//     attached — it only waits for tool calls.
//   - Stdio mode (retained test/agent scaffolding): newline-delimited
//     JSON-RPC 2.0 over stdin/stdout.
//
// Mode selection:
//   - explicit --stdio flag  → stdio
//   - stdin is a pipe/FIFO   → stdio (backwards compatibility: the Agent and
//     the pytest harness exec these binaries with no arguments over pipes)
//   - otherwise              → resident Unix-socket mode
//
// The socket directory is /run/c4 by default (systemd RuntimeDirectory=c4),
// overridable via the C4_SOCK_DIR environment variable or --sock-dir.
package transport

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"sync"
	"syscall"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// DefaultSockDir is the production socket directory; systemd units provide it
// via RuntimeDirectory=c4 (c4_deployment.md §6.5).
const DefaultSockDir = "/run/c4"

// Run parses transport flags and serves server until the transport ends.
// It blocks; a nil return means the server shut down cleanly.
func Run(serviceName string, server *mcp.Server) error {
	sockDirFlag := flag.String("sock-dir", "",
		"Unix socket directory for resident mode (default $C4_SOCK_DIR or "+DefaultSockDir+")")
	stdioFlag := flag.Bool("stdio", false,
		"serve MCP over stdin/stdout (newline-delimited JSON-RPC) instead of the Unix socket")
	flag.Parse()

	if *stdioFlag || stdinIsAttachedStream() {
		log.Printf("%s: stdio mode", serviceName)
		return server.Run(context.Background(), &mcp.StdioTransport{})
	}
	return serveResident(serviceName, resolveSockDir(*sockDirFlag), server)
}

func resolveSockDir(flagValue string) string {
	if flagValue != "" {
		return flagValue
	}
	if env := os.Getenv("C4_SOCK_DIR"); env != "" {
		return env
	}
	return DefaultSockDir
}

// stdinIsAttachedStream reports whether stdin is a FIFO or a Unix socket —
// the two forms a parent process hands us when it wants stdio JSON-RPC:
//   - FIFO: shell pipes and Python subprocess.Popen(stdin=PIPE) (pytest harness)
//   - socket: Node child_process stdio:'pipe' uses a socketpair (Agent)
//
// A character device stdin (interactive tty, or /dev/null under systemd) and
// a regular file both mean resident mode.
func stdinIsAttachedStream() bool {
	fi, err := os.Stdin.Stat()
	if err != nil {
		return false
	}
	mode := fi.Mode()
	return mode&os.ModeNamedPipe != 0 || mode&os.ModeSocket != 0
}

// serveResident listens on <dir>/<serviceName>.sock and serves one MCP
// session per accepted connection (c4_architecture.md §3.1.1: JSON-RPC 2.0
// over a Unix stream socket, initialize per connection). The listener is
// independent of the data-path tools: stop never affects it.
func serveResident(serviceName, dir string, server *mcp.Server) error {
	if err := os.MkdirAll(dir, 0o770); err != nil { //nolint:gosec // 0770 为 uid+group 访问边界（C4_RS_00015），刻意放宽
		return fmt.Errorf("create socket dir %s: %w", dir, err)
	}
	sockPath := filepath.Join(dir, serviceName+".sock")

	// unlink-before-bind: the systemd unit's singleton guarantee means a
	// leftover socket file is stale, never a live peer's (§3.1.1).
	if err := os.Remove(sockPath); err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("remove stale socket %s: %w", sockPath, err)
	}

	ln, err := net.Listen("unix", sockPath)
	if err != nil {
		return fmt.Errorf("listen on %s: %w", sockPath, err)
	}
	// Socket file permission is the uid-granular auth boundary (C4_RS_00015);
	// chmod explicitly so umask cannot strip the group bits.
	if err := os.Chmod(sockPath, 0o660); err != nil { //nolint:gosec // 0660 为 uid+group 访问边界（C4_RS_00015），刻意放宽
		_ = ln.Close()
		return fmt.Errorf("chmod %s: %w", sockPath, err)
	}
	log.Printf("%s: resident mode, listening on %s", serviceName, sockPath)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		_ = ln.Close()
	}()

	for {
		conn, err := ln.Accept()
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				_ = os.Remove(sockPath)
				return nil
			}
			log.Printf("%s: accept: %v", serviceName, err)
			continue
		}
		c := &onceConn{Conn: conn}
		go func() {
			defer func() { _ = c.Close() }()
			if err := server.Run(context.Background(), &mcp.IOTransport{Reader: c, Writer: c}); err != nil {
				log.Printf("%s: session ended: %v", serviceName, err)
			}
		}()
	}
}

// onceConn makes Close idempotent: IOTransport closes Reader and Writer
// independently, and both wrap the same net.Conn.
type onceConn struct {
	net.Conn
	once sync.Once
}

func (c *onceConn) Close() error {
	var err error
	c.once.Do(func() { err = c.Conn.Close() })
	return err
}
