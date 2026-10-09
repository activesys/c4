package main

// validate_points 工具（C4_FUN_00090）——契约: agent.md §2.7.1；规则: c4_influxdb_client.md §validate_points。
// type="" 两路径均放行（校验层无路径差异）：启动期按实际值推导运行期编码，
// 方案期由方案层推导填充保证（agent.md §2.7.1 确定性推导）。
// field 于 2026-10-01 裁定必填、无默认值、不推导（c4_influxdb_client.md §2）——
// 两路径均不再容忍空 field（FIELD_FORMAT 覆盖空值），运行期 resolveField 回退已废除。
// 校验逻辑与启动校验同源（validation.go validatePointSet，opts 参数化），本文件只做
// pointIssue → infIssue JSON 的渲染。

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type infIssue struct {
	Code    string   `json:"code"`
	Message string   `json:"message"`
	Points  []string `json:"points"`
	Field   string   `json:"field,omitempty"`
}

func validateInfluxPoints(points []influxPoint) []infIssue {
	raw := validatePointSet(points, pointValidateOpts{dupByBusinessKey: true})
	issues := []infIssue{}
	for _, is := range raw {
		switch is.Code {
		case "MEASUREMENT_EMPTY":
			issues = append(issues, infIssue{Code: "MEASUREMENT_EMPTY",
				Message: fmt.Sprintf("点 %s 的 measurement 为空", is.Key),
				Points:  []string{is.Key}, Field: "measurement"})
		case "INVALID_TYPE":
			issues = append(issues, infIssue{Code: "INVALID_TYPE",
				Message: fmt.Sprintf("点 %s 的 type='%s' 非法（float/int/uint/bool）", is.Key, is.Type),
				Points:  []string{is.Key}, Field: "type"})
		case "MEASUREMENT_FORMAT":
			issues = append(issues, infIssue{Code: "MEASUREMENT_FORMAT",
				Message: fmt.Sprintf("点 %s 的 measurement='%s' 含非法字符（仅英文字母/数字/下划线/点/连字符，不得中文）", is.Key, is.Measurement),
				Points:  []string{is.Key}, Field: "measurement"})
		case "FIELD_EMPTY":
			issues = append(issues, infIssue{Code: "FIELD_FORMAT",
				Message: fmt.Sprintf("点 %s 的 field 为空（必填，无默认值、不推导——由点表/用户提供）", is.Key),
				Points:  []string{is.Key}, Field: "field"})
		case "FIELD_FORMAT":
			issues = append(issues, infIssue{Code: "FIELD_FORMAT",
				Message: fmt.Sprintf("点 %s 的 field='%s' 含非法字符（仅字母与下划线）", is.Key, is.Field),
				Points:  []string{is.Key}, Field: "field"})
		case "TAG_FORMAT":
			issues = append(issues, infIssue{Code: "FIELD_FORMAT",
				Message: fmt.Sprintf("点 %s 的 tag key='%s' 含非法字符", is.Key, is.Field),
				Points:  []string{is.Key}, Field: "tags"})
		case "POINT_DUP":
			issues = append(issues, infIssue{Code: "POINT_DUP",
				Message: fmt.Sprintf("点 %s 与 %s 的 (measurement=%s, field=%s) 组合重复",
					is.PrevKey, is.Key, is.Measurement, is.Field),
				Points: []string{is.PrevKey, is.Key}, Field: "measurement"})
		}
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
