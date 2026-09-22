package main

import "testing"

func TestValidateASFClientPoints(t *testing.T) {
	issues := validateASFPoints([]clientPoint{
		{Key: "a.x", Addr: 1000},
		{Key: "b.x", Addr: 1000},
		{Key: "c.x", Addr: 17000000},
	})
	codes := map[string]bool{}
	for _, is := range issues {
		codes[is.Code] = true
	}
	if !codes["POINT_DUP"] || !codes["ADDR_OUT_OF_RANGE"] {
		t.Fatalf("got %v", issues)
	}
}
