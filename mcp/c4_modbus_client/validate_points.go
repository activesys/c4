package main

// validate_points 工具（C4_FUN_00086）—— L2 协议语义校验（方案期前置）。
// 契约: agent.md §2.7.1；规则表: c4_modbus_client.md §6 validate_points。
// 与启动校验（validateConfig/INVALID_POINT）同源——两者调用同一校验函数
// validatePointsCore，以 opts.RequireShmID 参数化子集处理 shm_id 差异。

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// PointIssue 单条校验违例（统一返回结构 agent.md §2.7.1）。
type PointIssue struct {
	Code    string   `json:"code"`
	Message string   `json:"message"`
	Points  []string `json:"points"`
	Field   string   `json:"field,omitempty"`
}

// ValidateOpts 参数化子集：启动路径 RequireShmID=true，validate_points=false。
type ValidateOpts struct {
	RequireShmID bool
}

// validatePointsCore 点表校验唯一实现（与 validateConfig 的点级规则一一对应，
// 见 main.go validateConfig——规则演进在此处同步两路径）。
// 返回全部违例（不 fail-fast，方案期一次报全）。
func validatePointsCore(points []modbusPoint, opts ValidateOpts) []PointIssue {
	issues := []PointIssue{}

	seen := make(map[modbusAddr]string)
	// 同 uid+fun 分组做区间重叠检测（与 validateConfig 相同的排序相邻算法）
	groups := make(map[[2]int][]modbusPoint)

	for _, pt := range points {
		if opts.RequireShmID && pt.ShmID == 0 {
			issues = append(issues, PointIssue{Code: "SHM_ID_NOT_ASSIGNED",
				Message: fmt.Sprintf("点 %s 的 shm_id=0，必须先由 c4_shm_manager 分配", pt.ID),
				Points:  []string{pt.ID}, Field: "shm_id"})
		}
		if pt.UID < 0 || pt.UID > 255 {
			issues = append(issues, PointIssue{Code: "UID_OUT_OF_RANGE",
				Message: fmt.Sprintf("点 %s 的 uid=%d 非法（须为 0~255）", pt.ID, pt.UID),
				Points:  []string{pt.ID}, Field: "uid"})
		}
		if pt.Addr > 0xFFFF {
			issues = append(issues, PointIssue{Code: "ADDR_OUT_OF_RANGE",
				Message: fmt.Sprintf("点 %s 的 addr=%d 非法（须为 0~65535）", pt.ID, pt.Addr),
				Points:  []string{pt.ID}, Field: "addr"})
		}
		if pt.Fun != 1 && pt.Fun != 2 && pt.Fun != 3 && pt.Fun != 4 {
			issues = append(issues, PointIssue{Code: "FUN_TYPE_MISMATCH",
				Message: fmt.Sprintf("点 %s 的 fun=%d 非法（须为 1/2/3/4）", pt.ID, pt.Fun),
				Points:  []string{pt.ID}, Field: "fun"})
		} else if !validTypeForFun(pt.Fun, pt.Type) {
			issues = append(issues, PointIssue{Code: "FUN_TYPE_MISMATCH",
				Message: fmt.Sprintf("点 %s 的 type=%d 与 fun=%d 不兼容", pt.ID, pt.Type, pt.Fun),
				Points:  []string{pt.ID}, Field: "type"})
		}

		span := int(pointSpan(pt.Fun, pt.Type))
		byteCount := span * 2
		if pt.Fun == 1 || pt.Fun == 2 {
			byteCount = 1
		}
		if pt.Swap != 0 && pt.Swap != 1 && pt.Swap != 2 && pt.Swap != 4 {
			issues = append(issues, PointIssue{Code: "BAD_SWAP",
				Message: fmt.Sprintf("点 %s 的 swap=%d 非法（合法值 0/1/2/4）", pt.ID, pt.Swap),
				Points:  []string{pt.ID}, Field: "swap"})
		} else if byteCount == 1 || byteCount == 2 {
			if pt.Swap != 0 {
				issues = append(issues, PointIssue{Code: "BAD_SWAP",
					Message: fmt.Sprintf("点 %s 为单数据单元（type=%d），swap 必须为 0（当前 %d）", pt.ID, pt.Type, pt.Swap),
					Points:  []string{pt.ID}, Field: "swap"})
			}
		} else if pt.Swap > 0 && byteCount%pt.Swap != 0 {
			issues = append(issues, PointIssue{Code: "BAD_SWAP",
				Message: fmt.Sprintf("点 %s 的 swap=%d 不整除字节跨度 %d", pt.ID, pt.Swap, byteCount),
				Points:  []string{pt.ID}, Field: "swap"})
		}

		key := modbusAddr{UID: uint8(pt.UID), Fun: pt.Fun, Addr: pt.Addr}
		if prev, ok := seen[key]; ok {
			issues = append(issues, PointIssue{Code: "POINT_DUP",
				Message: fmt.Sprintf("点 %s 与 %s 的 (uid=%d,fun=%d,addr=%d) 完全重复", pt.ID, prev, pt.UID, pt.Fun, pt.Addr),
				Points:  []string{prev, pt.ID}, Field: "addr"})
		}
		seen[key] = pt.ID
		groups[[2]int{pt.UID, int(pt.Fun)}] = append(groups[[2]int{pt.UID, int(pt.Fun)}], pt)
	}

	for gkey, pts := range groups {
		if len(pts) < 2 {
			continue
		}
		sorted := make([]modbusPoint, len(pts))
		copy(sorted, pts)
		sort.Slice(sorted, func(i, j int) bool { return sorted[i].Addr < sorted[j].Addr })
		for i := 1; i < len(sorted); i++ {
			prev := sorted[i-1]
			prevSpan := pointSpan(uint8(gkey[1]), prev.Type)
			if sorted[i].Addr < prev.Addr+uint32(prevSpan) {
				issues = append(issues, PointIssue{Code: "POINT_OVERLAP",
					Message: fmt.Sprintf("点 %s（addr=%d）与 %s（addr=%d，跨度 %d）在组 (uid=%d,fun=%d) 内地址区间重叠",
						sorted[i].ID, sorted[i].Addr, prev.ID, prev.Addr, prevSpan, gkey[0], gkey[1]),
					Points: []string{prev.ID, sorted[i].ID}, Field: "addr"})
			}
		}
	}
	return issues
}

// validatePointsInput 统一入参 schema（agent.md §2.7.1：全量点表，无状态）。
type validatePointsInput struct {
	Points []modbusPoint `json:"points"`
}

// validatePointsResult 统一返回结构。
type validatePointsResult struct {
	Valid    bool         `json:"valid"`
	Errors   []PointIssue `json:"errors"`
	Warnings []PointIssue `json:"warnings"`
}

func validatePointsHandler(ctx context.Context, req *mcp.CallToolRequest, input validatePointsInput) (*mcp.CallToolResult, any, error) {
	issues := validatePointsCore(input.Points, ValidateOpts{RequireShmID: false})
	result := validatePointsResult{Valid: len(issues) == 0, Errors: issues, Warnings: []PointIssue{}}
	b, _ := json.Marshal(result)
	return newResult(string(b)), nil, nil
}
