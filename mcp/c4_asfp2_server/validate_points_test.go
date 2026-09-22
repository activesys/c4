package main

import "testing"

func TestValidateASFPPoints(t *testing.T) {
	issues := validateASFPoints([]serverPoint{
		{ID: "a", Addr: 1000},
		{ID: "b", Addr: 1000}, // 重复 key → 静默覆盖前置拦截
		{ID: "c", Addr: 17000000},
	})
	codes := map[string]bool{}
	for _, is := range issues {
		codes[is.Code] = true
	}
	if !codes["POINT_DUP"] || !codes["ADDR_OUT_OF_RANGE"] {
		t.Fatalf("期望 POINT_DUP+ADDR_OUT_OF_RANGE, got %v", issues)
	}
	ok := validateASFPoints([]serverPoint{{ID: "a", Addr: 1000}, {ID: "b", Addr: 1001}})
	if len(ok) != 0 {
		t.Fatalf("合法点表不应报错: %v", ok)
	}
}
