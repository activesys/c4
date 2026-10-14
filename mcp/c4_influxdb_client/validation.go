package main

// 共享点级校验（c4_influxdb_client.md §validate_points「实现要求」：validate_points
// 与启动校验调用同一校验函数，禁止另写第二套逻辑；行为差异以 opts 参数化建模，
// agent.md §2.7.1 同源实现口径）。新增校验规则一律在本函数演进，两路径自动同步。
//
//   启动路径 validateConfig：requireShmID=true（shm_id 执行期由 c4_shm_manager 回填、
//   恒非 0，方案期恒为 0 不校验）+ dupByShmID=true（启动校验以 shm_id 查重为等价物）；
//   工具路径 validateInfluxPoints：requireShmID=false + dupByBusinessKey=true
//   （(measurement,field) 业务键查重）。

import (
	"regexp"
	"sort"
	"strings"
)

var (
	// fieldRe：field 合法字符集——2026-10-13 用户裁定放宽：字母/下划线开头、
	// 允许数字编号（AZGF 入库点表 yx0001/yc0001 形态；原 2026-10-09 口径
	// [a-zA-Z_]+），不得中文
	fieldRe = regexp.MustCompile("^[a-zA-Z_][a-zA-Z0-9_]*$")
	// keyRe：tag key 合法字符集（[a-zA-Z_]+ 维持不变，tags 值可为任意字符串）
	keyRe = regexp.MustCompile("^[a-zA-Z_]+$")
	// measRe：measurement 合法字符集（ASCII 字母/数字/下划线/点/连字符）——
	// 点表标识字段不得中文（2026-10-09 用户裁定；line protocol 兼容、展示无损）
	measRe = regexp.MustCompile("^[A-Za-z0-9_.-]+$")
)

type pointIssue struct {
	Code        string // SHM_ID | INVALID_TYPE | MEASUREMENT_EMPTY | MEASUREMENT_FORMAT | FIELD_EMPTY | FIELD_FORMAT | TAG_FORMAT | DUP_SHM | POINT_DUP
	Key         string
	Type        string // INVALID_TYPE：非法的 type 值
	Field       string // FIELD_FORMAT/TAG_FORMAT：非法的 field/tag key 值；POINT_DUP：撞键的 field 值
	Measurement string // POINT_DUP：撞键的 measurement 值
	ShmID       int    // DUP_SHM：重复的 shm_id
	PrevKey     string // POINT_DUP：先前置的点 key
}

type pointValidateOpts struct {
	requireShmID     bool
	dupByShmID       bool
	dupByBusinessKey bool
}

// validatePointSet 点级字段校验（顺序：shm_id → type → measurement → field → tags
// → 查重，§5.1/§6）；返回全部问题（启动路径由调用方取首个渲染为 error）。
func validatePointSet(points []influxPoint, opts pointValidateOpts) []pointIssue {
	issues := []pointIssue{}
	seenShm := make(map[int]bool)
	seenMF := make(map[[3]string]string)
	for _, pt := range points {
		if opts.requireShmID && pt.ShmID == 0 {
			issues = append(issues, pointIssue{Code: "SHM_ID", Key: pt.Key})
		}
		if pt.Type != "" && pt.Type != "float" && pt.Type != "int" && pt.Type != "uint" && pt.Type != "bool" {
			issues = append(issues, pointIssue{Code: "INVALID_TYPE", Key: pt.Key, Type: pt.Type})
		}
		if pt.Measurement == "" {
			issues = append(issues, pointIssue{Code: "MEASUREMENT_EMPTY", Key: pt.Key})
		} else if !measRe.MatchString(pt.Measurement) {
			issues = append(issues, pointIssue{Code: "MEASUREMENT_FORMAT", Key: pt.Key, Measurement: pt.Measurement})
		}
		// field 必填（2026-10-01 裁定：无默认值、不推导，废除空值放行与运行期回退）
		if pt.Field == "" {
			issues = append(issues, pointIssue{Code: "FIELD_EMPTY", Key: pt.Key})
		} else if !fieldRe.MatchString(pt.Field) {
			issues = append(issues, pointIssue{Code: "FIELD_FORMAT", Key: pt.Key, Field: pt.Field})
		}
		for k := range pt.Tags {
			if !keyRe.MatchString(k) {
				issues = append(issues, pointIssue{Code: "TAG_FORMAT", Key: pt.Key, Field: k})
			}
		}
		if opts.dupByShmID {
			if seenShm[pt.ShmID] {
				issues = append(issues, pointIssue{Code: "DUP_SHM", Key: pt.Key, ShmID: pt.ShmID})
			}
			seenShm[pt.ShmID] = true
		}
		if opts.dupByBusinessKey {
			// 业务键 = (measurement, tags, field)——series 身份含 tag set：
			// 同 measurement+field 不同 tag set 属不同 series（2026-10-13 裁定：
			// AZGF 入库点表 jd 恒值 measurement + device_id tag 区分 16 台装置，
			// 跨台同名 field 合法共存）；无 tags 点退化为 (measurement, "", field)
			// 与原口径兼容
			mk := [3]string{pt.Measurement, canonTags(pt.Tags), pt.Field}
			if prev, ok := seenMF[mk]; ok {
				issues = append(issues, pointIssue{
					Code: "POINT_DUP", Key: pt.Key, PrevKey: prev,
					Measurement: pt.Measurement, Field: pt.Field,
				})
			}
			seenMF[mk] = pt.Key
		}
	}
	return issues
}

// canonTags tags 规范化：键排序后 k=v 逗号串（series 身份的 tag set 分量）
func canonTags(tags map[string]string) string {
	keys := make([]string, 0, len(tags))
	for k := range tags {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, k+"="+tags[k])
	}
	return strings.Join(parts, ",")
}
