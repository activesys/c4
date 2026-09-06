package main

import (
	"context"
	"os"

	"c4/mcp/internal/logger"
)

func main() {
	l := logger.Init("c4_test_svc")
	ilog := l.With("inst", "hnals_test")
	ilog.Info("hello")
	ilog.Log(context.Background(), logger.LevelCrit, "crit-event")
	_ = os.Stderr
}
