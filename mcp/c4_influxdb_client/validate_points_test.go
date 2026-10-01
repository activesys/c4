package main

import "testing"

func TestValidateInfluxPoints(t *testing.T) {
	issues := validateInfluxPoints([]influxPoint{
		{Key: "k1", Measurement: "m", Field: "温度"},            // FIELD_FORMAT
		{Key: "k2", Measurement: "", Field: "f"},               // MEASUREMENT_EMPTY
		{Key: "k3", Measurement: "m", Field: "f", Type: "str"}, // INVALID_TYPE
		{Key: "k4", Measurement: "m", Field: "f"},              // POINT_DUP with k3? field 相同
	})
	codes := map[string]bool{}
	for _, is := range issues {
		codes[is.Code] = true
	}
	if !codes["FIELD_FORMAT"] || !codes["MEASUREMENT_EMPTY"] || !codes["INVALID_TYPE"] || !codes["POINT_DUP"] {
		t.Fatalf("期望四类错误, got %v", issues)
	}
	ok := validateInfluxPoints([]influxPoint{{Key: "k", Measurement: "m", Field: "f", Type: "float"}})
	if len(ok) != 0 {
		t.Fatalf("合法点不应报错: %v", ok)
	}
}

// field 必填（2026-10-01 裁定：无默认值、不推导，废除空值放行与运行期回退点名）
func TestValidateInfluxPointsFieldRequired(t *testing.T) {
	issues := validateInfluxPoints([]influxPoint{{Key: "k", Measurement: "m"}})
	if len(issues) != 1 || issues[0].Code != "FIELD_FORMAT" {
		t.Fatalf("空 field 应报 FIELD_FORMAT, got %v", issues)
	}
}
