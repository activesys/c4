# C4 — 静态代码检查统一入口
#
# `make lint`  = golangci-lint（mcp/ 全部 Go 模块）+ agent typecheck（tsc --noEmit）+ agent eslint
# `make lint-fix` = 自动修复（golangci-lint run --fix + eslint --fix），修复后请重新 `make lint` 确认
#
# Go 代码检查配置：mcp/.golangci.yml；TS 检查配置：agent/eslint.config.mjs + agent/tsconfig.json
# 说明：mcp/ 下每个目录都是独立 Go module，golangci-lint 需进入各模块目录运行，
# 配置文件 mcp/.golangci.yml 由其自动向上查找获得。

GOLANGCI_LINT ?= $(shell command -v golangci-lint 2>/dev/null || echo "$$(go env GOPATH)/bin/golangci-lint")

GO_MODULES := $(shell find mcp -name go.mod | xargs -n1 dirname | sort)

.PHONY: lint lint-fix

lint:
	@set -e; for m in $(GO_MODULES); do \
		echo "== golangci-lint: $$m"; \
		(cd $$m && $(GOLANGCI_LINT) run); \
	done
	@echo "== agent typecheck (tsc --noEmit)"
	npm --prefix agent run typecheck
	@echo "== agent lint (eslint)"
	npm --prefix agent run lint

lint-fix:
	@set -e; for m in $(GO_MODULES); do \
		echo "== golangci-lint --fix: $$m"; \
		(cd $$m && $(GOLANGCI_LINT) run --fix); \
	done
	@echo "== agent lint --fix (eslint)"
	npm --prefix agent run lint -- --fix
	@echo "== done; re-run 'make lint' to confirm"
