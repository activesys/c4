// Package logger provides the shared journald-oriented structured logger for
// C4 MCP services. Log lines are written to stderr with a "<N>" syslog
// priority prefix so systemd's SyslogLevelPrefix maps them to native journal
// PRIORITY fields (crit=2, err=3, warning=4, info=6, debug=7). Configured via
// environment variables (see docs/design/c4_asfp2_server.md §9):
//
//	C4_MCP_LOG_LEVEL          crit|err|warning|info|debug  (default info)
//	C4_MCP_LOG_FORMAT         text|json                    (default text)
//	C4_MCP_LOG_STATS_INTERVAL seconds, 0 disables          (default 60)
package logger

import (
	"bytes"
	"context"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

// LevelCrit matches journald PRIORITY=2; slog has no crit level.
const LevelCrit = slog.LevelError + 4

var statsInterval time.Duration

// Init reads the C4_MCP_LOG_* environment variables, installs the priority
// prefixed handler as the default slog logger and returns a logger with the
// service name attached. Call once at the top of main().
func Init(service string) *slog.Logger {
	level, levelOK := parseLevel(os.Getenv("C4_MCP_LOG_LEVEL"), slog.LevelInfo)
	format := strings.ToLower(os.Getenv("C4_MCP_LOG_FORMAT"))
	formatOK := format == "" || format == "text" || format == "json"
	ivRaw := os.Getenv("C4_MCP_LOG_STATS_INTERVAL")
	iv, ivOK := parseInterval(ivRaw, 60*time.Second)
	statsInterval = iv

	opts := &slog.HandlerOptions{Level: level}
	h := &prioHandler{mu: &sync.Mutex{}, buf: &bytes.Buffer{}}
	if format == "json" {
		h.inner = slog.NewJSONHandler(h.buf, opts)
	} else {
		h.inner = slog.NewTextHandler(h.buf, opts)
	}

	l := slog.New(h).With("svc", service)
	slog.SetDefault(l)
	if os.Getenv("C4_MCP_LOG_LEVEL") != "" && !levelOK {
		l.Warn("logger_config_fallback", "var", "C4_MCP_LOG_LEVEL",
			"value", os.Getenv("C4_MCP_LOG_LEVEL"), "fallback", "info")
	}
	if os.Getenv("C4_MCP_LOG_FORMAT") != "" && !formatOK {
		l.Warn("logger_config_fallback", "var", "C4_MCP_LOG_FORMAT",
			"value", os.Getenv("C4_MCP_LOG_FORMAT"), "fallback", "text")
	}
	if ivRaw != "" && !ivOK {
		l.Warn("logger_config_fallback", "var", "C4_MCP_LOG_STATS_INTERVAL",
			"value", ivRaw, "fallback", "60")
	}
	return l
}

// StatsInterval returns the configured periodic-stats interval (0 = disabled).
func StatsInterval() time.Duration { return statsInterval }

// prioHandler serializes inner-handler formatting into a shared buffer,
// prepends the "<N>" priority tag and writes the line to stderr.
type prioHandler struct {
	inner slog.Handler
	mu    *sync.Mutex
	buf   *bytes.Buffer
}

func (h *prioHandler) Enabled(ctx context.Context, l slog.Level) bool {
	return h.inner.Enabled(ctx, l)
}

func (h *prioHandler) Handle(ctx context.Context, r slog.Record) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.buf.Reset()
	if err := h.inner.Handle(ctx, r); err != nil {
		return err
	}
	out := make([]byte, 0, 4+h.buf.Len())
	out = append(out, priorityTag(r.Level)...)
	out = append(out, h.buf.Bytes()...)
	_, err := os.Stderr.Write(out)
	return err
}

func (h *prioHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	return &prioHandler{inner: h.inner.WithAttrs(attrs), mu: h.mu, buf: h.buf}
}

func (h *prioHandler) WithGroup(name string) slog.Handler {
	return &prioHandler{inner: h.inner.WithGroup(name), mu: h.mu, buf: h.buf}
}

// priorityTag maps a slog level onto the syslog/journald priority tag.
func priorityTag(l slog.Level) string {
	switch {
	case l >= LevelCrit:
		return "<2>"
	case l >= slog.LevelError:
		return "<3>"
	case l >= slog.LevelWarn:
		return "<4>"
	case l >= slog.LevelInfo:
		return "<6>"
	default:
		return "<7>"
	}
}

// parseLevel maps a C4_MCP_LOG_LEVEL value onto a slog level. ok reports
// whether the value was recognized; ok=false with def as the fallback.
func parseLevel(s string, def slog.Level) (level slog.Level, ok bool) {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "crit":
		return LevelCrit, true
	case "err", "error":
		return slog.LevelError, true
	case "warning", "warn":
		return slog.LevelWarn, true
	case "info":
		return slog.LevelInfo, true
	case "debug":
		return slog.LevelDebug, true
	default:
		return def, false
	}
}

// parseInterval maps a C4_MCP_LOG_STATS_INTERVAL value (seconds) onto a
// duration. ok reports whether the value was recognized; ok=false with def as
// the fallback. "0" is a valid value (disables stats) and returns (0, true).
func parseInterval(s string, def time.Duration) (time.Duration, bool) {
	s = strings.TrimSpace(s)
	if s == "" {
		return def, false
	}
	sec, err := strconv.Atoi(s)
	if err != nil || sec < 0 {
		return def, false
	}
	return time.Duration(sec) * time.Second, true
}
