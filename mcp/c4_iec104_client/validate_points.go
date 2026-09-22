package main

// validate_points 工具（C4_FUN_00087）——契约: agent.md §2.7.1；规则: c4_iec104_client.md §6。
// ADDR_OUT_OF_RANGE 依赖实例级 ioa_size，归启动校验（工具参数不含实例字段）。

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type iecIssue struct {
	Code    string   `json:"code"`
	Message string   `json:"message"`
	Points  []string `json:"points"`
	Field   string   `json:"field,omitempty"`
}

func validateIECPoints(points []iec104Point) []iecIssue {
	issues := []iecIssue{}
	seen := make(map[uint32]string)
	for _, pt := range points {
		if prev, ok := seen[pt.Addr]; ok {
			issues = append(issues, iecIssue{Code: "POINT_DUP",
				Message: fmt.Sprintf("点 %s 与 %s 的信息对象地址 addr=%d 重复", prev, pt.ID, pt.Addr),
				Points:  []string{prev, pt.ID}, Field: "addr"})
		}
		seen[pt.Addr] = pt.ID
	}
	return issues
}

type iecVInput struct {
	Points []iec104Point `json:"points"`
}
type iecVResult struct {
	Valid    bool       `json:"valid"`
	Errors   []iecIssue `json:"errors"`
	Warnings []iecIssue `json:"warnings"`
}

func validatePointsHandler(ctx context.Context, req *mcp.CallToolRequest, input iecVInput) (*mcp.CallToolResult, any, error) {
	issues := validateIECPoints(input.Points)
	b, _ := json.Marshal(iecVResult{Valid: len(issues) == 0, Errors: issues, Warnings: []iecIssue{}})
	return newResult(string(b)), nil, nil
}
