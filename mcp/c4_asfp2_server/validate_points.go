package main

// validate_points 工具（C4_FUN_00089）——契约: agent.md §2.7.1；规则: c4_asfp2_server.md §validate_points。
// POINT_DUP 检测在共享校验路径实现，启动校验随之获得该检查（修复现状映射静默覆盖缺口）。

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"c4/mcp/internal/protocol"
)

type asfpIssue struct {
	Code    string   `json:"code"`
	Message string   `json:"message"`
	Points  []string `json:"points"`
	Field   string   `json:"field,omitempty"`
}

func validateASFPoints(points []serverPoint) []asfpIssue {
	issues := []asfpIssue{}
	seen := make(map[uint32]string)
	for _, pt := range points {
		id := pt.ID
		if pt.Addr > protocol.MaxAddr {
			issues = append(issues, asfpIssue{Code: "ADDR_OUT_OF_RANGE",
				Message: fmt.Sprintf("点 %s 的 addr=%d 超出协议上限 %d", id, pt.Addr, protocol.MaxAddr),
				Points:  []string{id}, Field: "addr"})
		}
		if prev, ok := seen[pt.Addr]; ok {
			issues = append(issues, asfpIssue{Code: "POINT_DUP",
				Message: fmt.Sprintf("点 %s 与 %s 的 addr（ASFP2 key）=%d 重复——addr → shm_id 反向映射 构建时后者静默覆盖前者", prev, id, pt.Addr),
				Points:  []string{prev, id}, Field: "addr"})
		}
		seen[pt.Addr] = id
	}
	return issues
}

type asfpVInput struct {
	Points []serverPoint `json:"points"`
}
type asfpVResult struct {
	Valid    bool        `json:"valid"`
	Errors   []asfpIssue `json:"errors"`
	Warnings []asfpIssue `json:"warnings"`
}

func validatePointsHandler(ctx context.Context, req *mcp.CallToolRequest, input asfpVInput) (*mcp.CallToolResult, any, error) {
	issues := validateASFPoints(input.Points)
	b, _ := json.Marshal(asfpVResult{Valid: len(issues) == 0, Errors: issues, Warnings: []asfpIssue{}})
	return newResult(string(b)), nil, nil
}
