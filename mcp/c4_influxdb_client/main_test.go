package main

import (
	"strings"
	"testing"
)

func validInstance() influxInstance {
	batch := 100
	flush := 1
	return influxInstance{
		Name:          "inst",
		URL:           "http://127.0.0.1:8086",
		Token:         "tk",
		Org:           "activesys",
		Bucket:        "data",
		BatchSize:     &batch,
		FlushInterval: &flush,
		Points: []influxPoint{
			{Key: "channel1.wt1_windspeed", Measurement: "wind", Field: "windspeed", Type: "float", ShmID: 1},
		},
	}
}

// 合法配置 → nil（基线）
func TestValidateConfigValid(t *testing.T) {
	if err := validateConfig([]influxInstance{validInstance()}); err != nil {
		t.Fatalf("合法配置不应报错: %v", err)
	}
}

// field 必填（2026-10-01 裁定）：空 field → INVALID_POINT: empty field
func TestValidateConfigEmptyField(t *testing.T) {
	inst := validInstance()
	inst.Points[0].Field = ""
	err := validateConfig([]influxInstance{inst})
	if err == nil || err.Error() != "INVALID_POINT: point 'channel1.wt1_windspeed' has empty field" {
		t.Fatalf("空 field 应报 INVALID_POINT empty field, got %v", err)
	}
}

// field 非法字符 → INVALID_POINT: invalid field
func TestValidateConfigInvalidField(t *testing.T) {
	inst := validInstance()
	inst.Points[0].Field = "温 度"
	err := validateConfig([]influxInstance{inst})
	if err == nil || err.Error() != "INVALID_POINT: point 'channel1.wt1_windspeed' has invalid field '温 度'" {
		t.Fatalf("非法 field 应报 INVALID_POINT invalid field, got %v", err)
	}
}

// shm_id 未分配优先于实例字段与点级检查（§5.1 校验顺序）
func TestValidateConfigShmFirst(t *testing.T) {
	inst := validInstance()
	inst.URL = ""
	inst.Points[0].ShmID = 0
	inst.Points[0].Field = ""
	err := validateConfig([]influxInstance{inst})
	if err == nil || !strings.HasPrefix(err.Error(), "SHM_ID_NOT_ASSIGNED") {
		t.Fatalf("shm_id 检查应最先报错, got %v", err)
	}
}

// 同实例 shm_id 查重
func TestValidateConfigDupShm(t *testing.T) {
	inst := validInstance()
	inst.Points = append(inst.Points, influxPoint{
		Key: "channel1.wt1_temperature", Measurement: "wind", Field: "temperature", Type: "float", ShmID: 1,
	})
	err := validateConfig([]influxInstance{inst})
	if err == nil || err.Error() != "INVALID_POINT: duplicate shm_id=1 in instance 'inst'" {
		t.Fatalf("重复 shm_id 应报 INVALID_POINT duplicate, got %v", err)
	}
}

// 实例字段缺失 → INVALID_CONFIG
func TestValidateConfigInstanceFields(t *testing.T) {
	inst := validInstance()
	inst.Token = ""
	err := validateConfig([]influxInstance{inst})
	if err == nil || err.Error() != "INVALID_CONFIG: instance 'inst' has empty token" {
		t.Fatalf("空 token 应报 INVALID_CONFIG, got %v", err)
	}
}

// encodeFieldValue 缺省类型映射（2026-10-16 用例 38）：uint 采集类型必须输出
// 整数字面量 "Ni"——行协议无 "u" 后缀，原 "Nu" 被 influxd 整批 400 丢弃
func TestEncodeFieldValueUnsignedDefault(t *testing.T) {
	var u32 uint32 = 3000
	got, ok := encodeFieldValue(u32, "")
	if !ok || got != "3000i" {
		t.Fatalf("uint32 缺省类型应输出 3000i，got %q ok=%v", got, ok)
	}
	var u64 uint64 = 1234567890
	got, ok = encodeFieldValue(u64, "")
	if !ok || got != "1234567890i" {
		t.Fatalf("uint64 缺省类型应输出整数字面量，got %q ok=%v", got, ok)
	}
	// 超 int64 上界退化为浮点（合法行协议，值域无损到 2^53）
	var big uint64 = 1 << 63
	got, ok = encodeFieldValue(big, "")
	if !ok || !strings.Contains(got, "e") && !strings.Contains(got, ".") {
		t.Fatalf("超界 uint64 应退化为浮点，got %q", got)
	}
	// float 采集类型保持浮点字面量
	got, ok = encodeFieldValue(float64(12.5), "")
	if !ok || got != "12.5" {
		t.Fatalf("float 缺省类型应输出 12.5，got %q", got)
	}
}
