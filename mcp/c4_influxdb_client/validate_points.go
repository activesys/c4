package main

// validate_points 工具（C4_FUN_00090）——契约: agent.md §2.7.1；规则: c4_influxdb_client.md §validate_points。
// 空值容忍差异：启动校验容忍 type=""/field=""（运行期推导/回退），本工具为方案期校验、
// 不容忍空串（推导填充由方案层完成，见 agent.md §2.7.1 确定性推导）。

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

var vKeyRe = regexp.MustCompile(`^[a-zA-Z_]+$`)

type infIssue struct {
	Code    string   `json:"code"`
	Message string   `json:"message"`
	Points  []string `json:"points"`
	Field   string   `json:"field,omitempty"`
}

func validateInfluxPoints(points []influxPoint) []infIssue {
	issues := []infIssue{}
	seen := make(map[[2]string]string)
	for _, pt := range points {
		if pt.Measurement == "" {
			issues = append(issues, infIssue{Code: "MEASUREMENT_EMPTY",
				Message: fmt.Sprintf("点 %s 的 measurement 为空", pt.Key),
				Points:  []string{pt.Key}, Field: "measurement"})
		}
		if pt.Type != "" && pt.Type != "float" && pt.Type != "int" && pt.Type != "uint" && pt.Type != "bool" {
			issues = append(issues, infIssue{Code: "INVALID_TYPE",
				Message: fmt.Sprintf("点 %s 的 type='%s' 非法（float/int/uint/bool）", pt.Key, pt.Type),
				Points:  []string{pt.Key}, Field: "type"})
		}
		if pt.Field != "" && !vKeyRe.MatchString(pt.Field) {
			issues = append(issues, infIssue{Code: "FIELD_FORMAT",
				Message: fmt.Sprintf("点 %s 的 field='%s' 含非法字符（仅字母与下划线）", pt.Key, pt.Field),
				Points:  []string{pt.Key}, Field: "field"})
		}
		for k := range pt.Tags {
			if !vKeyRe.MatchString(k) {
				issues = append(issues, infIssue{Code: "FIELD_FORMAT",
					Message: fmt.Sprintf("点 %s 的 tag key='%s' 含非法字符", pt.Key, k),
					Points:  []string{pt.Key}, Field: "tags"})
			}
		}
		mk := [2]string{pt.Measurement, pt.Field}
		if prev, ok := seen[mk]; ok {
			issues = append(issues, infIssue{Code: "POINT_DUP",
				Message: fmt.Sprintf("点 %s 与 %s 的 (measurement=%s, field=%s) 组合重复", prev, pt.Key, pt.Measurement, pt.Field),
				Points:  []string{prev, pt.Key}, Field: "measurement"})
		}
		seen[mk] = pt.Key
	}
	return issues
}

type infVInput struct {
	Points []influxPoint `json:"points"`
}
type infVResult struct {
	Valid    bool       `json:"valid"`
	Errors   []infIssue `json:"errors"`
	Warnings []infIssue `json:"warnings"`
}

func validatePointsHandler(ctx context.Context, req *mcp.CallToolRequest, input infVInput) (*mcp.CallToolResult, any, error) {
	issues := validateInfluxPoints(input.Points)
	b, _ := json.Marshal(infVResult{Valid: len(issues) == 0, Errors: issues, Warnings: []infIssue{}})
	return newResult(string(b)), nil, nil
}
