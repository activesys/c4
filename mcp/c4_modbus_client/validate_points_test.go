package main

import "testing"

func mkPoint(id string, uid, addr int, fun, typ uint8, swap int) modbusPoint {
	return modbusPoint{ID: id, UID: uid, Addr: uint32(addr), Fun: fun, Type: typ, Swap: swap}
}

func codes(issues []PointIssue) map[string]bool {
	m := map[string]bool{}
	for _, is := range issues {
		m[is.Code] = true
	}
	return m
}

func TestValidatePointsCore(t *testing.T) {
	// 合法点表（对齐 9/19 风机形态：fun=3 int16/float32，swap 合法）
	ok := validatePointsCore([]modbusPoint{
		mkPoint("windspeed", 1, 1000, 3, 10, 2),
		mkPoint("temperature", 1, 1002, 3, 10, 0),
		mkPoint("valve", 1, 3000, 1, 0, 0),
	}, ValidateOpts{RequireShmID: false})
	if len(ok) != 0 {
		t.Fatalf("合法点表应通过, got %v", ok)
	}

	// 重叠：3000(float32, span 2) 与 3001(float32) 区间重叠
	ov := codes(validatePointsCore([]modbusPoint{
		mkPoint("a", 1, 3000, 3, 10, 0),
		mkPoint("b", 1, 3001, 3, 10, 0),
	}, ValidateOpts{}))
	if !ov["POINT_OVERLAP"] {
		t.Fatal("重叠未检出 POINT_OVERLAP")
	}

	// 相邻不重叠：3000+3010
	adj := validatePointsCore([]modbusPoint{
		mkPoint("a", 1, 3000, 3, 10, 0),
		mkPoint("b", 1, 3010, 3, 10, 0),
	}, ValidateOpts{})
	for _, is := range adj {
		if is.Code == "POINT_OVERLAP" {
			t.Fatal("相邻点误报重叠")
		}
	}

	// 重复：同 uid+fun+addr
	dp := codes(validatePointsCore([]modbusPoint{
		mkPoint("a", 1, 1000, 3, 10, 0),
		mkPoint("a2", 1, 1000, 3, 10, 0),
	}, ValidateOpts{}))
	if !dp["POINT_DUP"] {
		t.Fatal("重复未检出 POINT_DUP")
	}

	// fun=1 线圈：type 必须 0/15，swap 必须 0
	ft := codes(validatePointsCore([]modbusPoint{mkPoint("a", 1, 3000, 1, 10, 0)}, ValidateOpts{}))
	if !ft["FUN_TYPE_MISMATCH"] {
		t.Fatal("线圈配 float32 未检出 FUN_TYPE_MISMATCH")
	}
	sw := codes(validatePointsCore([]modbusPoint{mkPoint("a", 1, 3000, 1, 0, 2)}, ValidateOpts{}))
	if !sw["BAD_SWAP"] {
		t.Fatal("线圈 swap=2 未检出 BAD_SWAP")
	}

	// 范围：uid 256 / addr 65536
	rg := codes(validatePointsCore([]modbusPoint{mkPoint("a", 256, 0, 3, 10, 0), mkPoint("b", 1, 65536, 3, 10, 0)}, ValidateOpts{}))
	if !rg["UID_OUT_OF_RANGE"] || !rg["ADDR_OUT_OF_RANGE"] {
		t.Fatal("范围校验缺失")
	}

	// requireShmID=true：shm_id=0 必须报
	sh := validatePointsCore([]modbusPoint{mkPoint("a", 1, 1000, 3, 10, 0)}, ValidateOpts{RequireShmID: true})
	if len(sh) == 0 || sh[0].Code != "SHM_ID_NOT_ASSIGNED" {
		t.Fatal("RequireShmID=true 未检出 shm_id=0")
	}
	// requireShmID=false：不报
	sh0 := validatePointsCore([]modbusPoint{mkPoint("a", 1, 1000, 3, 10, 0)}, ValidateOpts{})
	if len(sh0) != 0 {
		t.Fatal("RequireShmID=false 不应报 shm_id")
	}
}
