package api

import (
	"log/slog"
	"strconv"
)

func itoa(v int) string { return strconv.Itoa(v) }

func slogWarn(msg string, err error) { slog.Warn(msg, "err", err) }
