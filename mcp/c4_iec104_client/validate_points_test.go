package main

import "testing"

func TestValidateIECPoints(t *testing.T) {
	issues := validateIECPoints([]iec104Point{{ID: "a", Addr: 100}, {ID: "b", Addr: 100}})
	if len(issues) != 1 || issues[0].Code != "POINT_DUP" {
		t.Fatalf("期望 POINT_DUP, got %v", issues)
	}
	if len(validateIECPoints([]iec104Point{{ID: "a", Addr: 100}, {ID: "b", Addr: 101}})) != 0 {
		t.Fatal("合法点表不应报错")
	}
}
