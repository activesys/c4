package main

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"golang.org/x/sys/unix"

	"c4/mcp/internal/protocol"
	"c4/mcp/internal/shm"
)

// ──────────────────────────────────────────────
//  Configuration types
// ──────────────────────────────────────────────

type influxPoint struct {
	Key         string            `json:"key"`
	Measurement string            `json:"measurement"`
	Field       string            `json:"field"`
	Type        string            `json:"type"`
	Tags        map[string]string `json:"tags"`
	ShmID       int               `json:"shm_id"`
}

type influxInstance struct {
	Name          string        `json:"name"`
	ID            string        `json:"id"`
	URL           string        `json:"url"`
	Token         string        `json:"token"`
	Org           string        `json:"org"`
	Bucket        string        `json:"bucket"`
	Precision     string        `json:"precision"`
	BatchSize     *int          `json:"batch_size"`
	FlushInterval *int          `json:"flush_interval"`
	Timer         *int          `json:"timer"`
	Gzip          *int          `json:"gzip"`
	T0            *int          `json:"t0"`
	Retries       *int          `json:"retries"`
	Points        []influxPoint `json:"points"`
}

// ──────────────────────────────────────────────
//  Point mapping (internal index)
// ──────────────────────────────────────────────

type pointMapping struct {
	shmID       int
	measurement string
	field       string
	pointType   string // "" / "float" / "int" / "uint" / "bool"
	tags        map[string]string
}

// ──────────────────────────────────────────────
//  Instance / client state
// ──────────────────────────────────────────────

type instanceState struct {
	cfg         influxInstance
	points      []pointMapping
	lastSeen    map[int]uint64
	buffer      []string
	lastFlush   time.Time
	httpClient  *http.Client
	quit        chan struct{}
	wg          sync.WaitGroup
	writeErrors uint64
	itemsSkipped uint64
}

type clientState struct {
	started   atomic.Bool
	instances []*instanceState
	mu        sync.Mutex
	shmData   []byte
	shmFd     int
}

var state = &clientState{}

// ──────────────────────────────────────────────
//  Config loading + validation
// ──────────────────────────────────────────────

func loadConfig(configPath string) ([]influxInstance, error) {
	data, err := os.ReadFile(configPath)
	if err != nil {
		return nil, fmt.Errorf("CONFIG_PATH_MISSING: cannot read config file: %v", err)
	}

	var fullCfg map[string]any
	if err := json.Unmarshal(data, &fullCfg); err != nil {
		return nil, fmt.Errorf("CONFIG_PARSE_ERROR: failed to parse config JSON: %v", err)
	}

	section, ok := fullCfg["c4_influxdb_client"]
	if !ok {
		return nil, fmt.Errorf("CONFIG_PARSE_ERROR: 'c4_influxdb_client' section not found in config")
	}

	rawJSON, _ := json.Marshal(section)
	var instances []influxInstance
	if err := json.Unmarshal(rawJSON, &instances); err != nil {
		return nil, fmt.Errorf("CONFIG_PARSE_ERROR: failed to parse 'c4_influxdb_client' section: %v", err)
	}

	applyDefaults(instances)
	return instances, nil
}

func applyDefaults(instances []influxInstance) {
	for i := range instances {
		if instances[i].Precision == "" {
			instances[i].Precision = "ms"
		}
		if instances[i].BatchSize == nil {
			v := 5000
			instances[i].BatchSize = &v
		}
		if instances[i].FlushInterval == nil {
			v := 1000
			instances[i].FlushInterval = &v
		}
		if instances[i].Timer == nil {
			v := 100
			instances[i].Timer = &v
		}
		if instances[i].Gzip == nil {
			v := 1
			instances[i].Gzip = &v
		}
		if instances[i].T0 == nil {
			v := 30
			instances[i].T0 = &v
		}
		if instances[i].Retries == nil {
			v := 3
			instances[i].Retries = &v
		}
	}
}

var keyRe = regexp.MustCompile("^[a-zA-Z_]+$")

func isValidURL(s string) bool {
	u, err := url.Parse(s)
	if err != nil {
		return false
	}
	return u.Scheme == "http" || u.Scheme == "https"
}

// Validation order (§5.1, §6): shm_id (SHM_ID_NOT_ASSIGNED) → instance fields
// (INVALID_CONFIG) → point fields (INVALID_POINT).
func validateConfig(instances []influxInstance) error {
	for _, inst := range instances {
		// 1. shm_id must be assigned (non-zero)
		for _, pt := range inst.Points {
			if pt.ShmID == 0 {
				return fmt.Errorf("SHM_ID_NOT_ASSIGNED: point '%s' has shm_id=0, must be assigned by c4_shm_manager first", pt.Key)
			}
		}

		// 2. instance-level fields (INVALID_CONFIG)
		if inst.URL == "" {
			return fmt.Errorf("INVALID_CONFIG: instance '%s' has empty url", inst.Name)
		}
		if !isValidURL(inst.URL) {
			return fmt.Errorf("INVALID_CONFIG: instance '%s' has invalid url '%s'", inst.Name, inst.URL)
		}
		if inst.Token == "" {
			return fmt.Errorf("INVALID_CONFIG: instance '%s' has empty token", inst.Name)
		}
		if inst.Org == "" {
			return fmt.Errorf("INVALID_CONFIG: instance '%s' has empty org", inst.Name)
		}
		if inst.Bucket == "" {
			return fmt.Errorf("INVALID_CONFIG: instance '%s' has empty bucket", inst.Name)
		}
		if *inst.BatchSize <= 0 {
			return fmt.Errorf("INVALID_CONFIG: instance '%s' has invalid batch_size=%d (must be > 0)", inst.Name, *inst.BatchSize)
		}
		if *inst.FlushInterval < 0 {
			return fmt.Errorf("INVALID_CONFIG: instance '%s' has invalid flush_interval=%d (must be >= 0)", inst.Name, *inst.FlushInterval)
		}

		// 3. point-level fields (INVALID_POINT)
		seenShmIDs := make(map[int]bool)
		for _, pt := range inst.Points {
			if pt.Type != "" && pt.Type != "float" && pt.Type != "int" && pt.Type != "uint" && pt.Type != "bool" {
				return fmt.Errorf("INVALID_POINT: point '%s' has invalid type '%s'", pt.Key, pt.Type)
			}
			if pt.Measurement == "" {
				return fmt.Errorf("INVALID_POINT: point '%s' has empty measurement", pt.Key)
			}
			if pt.Field != "" && !keyRe.MatchString(pt.Field) {
				return fmt.Errorf("INVALID_POINT: point '%s' has invalid field '%s'", pt.Key, pt.Field)
			}
			for k := range pt.Tags {
				if !keyRe.MatchString(k) {
					return fmt.Errorf("INVALID_POINT: point '%s' has invalid tag key '%s'", pt.Key, k)
				}
			}
			if seenShmIDs[pt.ShmID] {
				return fmt.Errorf("INVALID_POINT: duplicate shm_id=%d in instance '%s'", pt.ShmID, inst.Name)
			}
			seenShmIDs[pt.ShmID] = true
		}
	}
	return nil
}

var instanceIDRe = regexp.MustCompile("^c4_[a-zA-Z0-9]+$")

func validateInstanceID(id string) bool {
	return instanceIDRe.MatchString(id)
}

// ──────────────────────────────────────────────
//  Shared memory (O_RDONLY)
// ──────────────────────────────────────────────

func attachShm(instanceID string) ([]byte, int, error) {
	shmPath := "/dev/shm/" + instanceID

	fd, err := unix.Open(shmPath, unix.O_RDONLY, 0)
	if err != nil {
		return nil, 0, fmt.Errorf("SHM_OPEN_FAILED: shm_open failed for %s: %v", shmPath, err)
	}

	hdrData, err := unix.Mmap(fd, 0, shm.BlockSize, unix.PROT_READ, unix.MAP_SHARED)
	if err != nil {
		unix.Close(fd)
		return nil, 0, fmt.Errorf("SHM_OPEN_FAILED: mmap header failed: %v", err)
	}
	magic := binary.NativeEndian.Uint32(hdrData[0:])
	if magic != shm.Magic {
		unix.Munmap(hdrData)
		unix.Close(fd)
		return nil, 0, fmt.Errorf("SHM_CORRUPTED: header magic is invalid (got 0x%08X, expected 0x%08X)", magic, shm.Magic)
	}
	maxPoints := binary.NativeEndian.Uint32(hdrData[shm.HdrOffMaxPoints:])
	unix.Munmap(hdrData)

	totalSize := int64(int(maxPoints)+1) * shm.BlockSize
	data, err := unix.Mmap(fd, 0, int(totalSize), unix.PROT_READ, unix.MAP_SHARED)
	if err != nil {
		unix.Close(fd)
		return nil, 0, fmt.Errorf("SHM_OPEN_FAILED: mmap failed: %v", err)
	}

	return data, fd, nil
}

// readBlock reads a single block from SHM using the seqlock protocol.
func readBlock(shmData []byte, shmID int) (uint8, uint64, uint64, uint64, bool) {
	off := shmID * shm.BlockSize
	if off+shm.BlockSize > len(shmData) {
		return 0, 0, 0, 0, false
	}

	if binary.NativeEndian.Uint32(shmData[off+shm.BlkOffMagic:]) != shm.Magic {
		return 0, 0, 0, 0, false
	}
	if shmData[off+shm.BlkOffState] == 0 {
		return 0, 0, 0, 0, false
	}

	for i := 0; i < 100; i++ {
		s1 := binary.NativeEndian.Uint64(shmData[off+shm.BlkOffWriteSeq:])
		if s1&1 != 0 {
			return 0, 0, 0, 0, false
		}
		dt := shmData[off+shm.BlkOffType]
		ts := binary.NativeEndian.Uint64(shmData[off+shm.BlkOffTimestamp:])
		val := binary.NativeEndian.Uint64(shmData[off+shm.BlkOffValue:])
		s2 := binary.NativeEndian.Uint64(shmData[off+shm.BlkOffWriteSeq:])
		if s1 == s2 {
			return dt, ts, val, s1, true
		}
		if i > 10 {
			runtime.Gosched()
		}
	}
	return 0, 0, 0, 0, false
}

// ──────────────────────────────────────────────
//  Type conversion (§4.4.1)
// ──────────────────────────────────────────────

// float16ToFloat32 expands an IEEE 754 half-precision bit pattern to float32.
func float16ToFloat32(h uint16) float32 {
	sign := uint32(h>>15) & 1
	exp := uint32(h>>10) & 0x1F
	mant := uint32(h) & 0x3FF
	var f32 uint32
	switch {
	case exp == 0:
		if mant == 0 {
			f32 = sign << 31
		} else {
			exp = 127 - 15 + 1
			for mant&0x400 == 0 {
				mant <<= 1
				exp--
			}
			mant &= 0x3FF
			f32 = sign<<31 | exp<<23 | mant<<13
		}
	case exp == 0x1F:
		f32 = sign<<31 | 0xFF<<23 | mant<<13
	default:
		f32 = sign<<31 | (exp-15+127)<<23 | mant<<13
	}
	return math.Float32frombits(f32)
}

// decodeAcquiredValue decodes the 64-bit value field per the acquired type.
// Returns nil,false for non-numeric types.
func decodeAcquiredValue(dataType uint8, value uint64) (interface{}, bool) {
	switch dataType {
	case protocol.TypeBoolean, protocol.TypeBit:
		return value&1 != 0, true
	case protocol.TypeInt8:
		return int8(value), true
	case protocol.TypeUint8:
		return uint8(value), true
	case protocol.TypeInt16:
		return int16(value), true
	case protocol.TypeUint16:
		return uint16(value), true
	case protocol.TypeInt32:
		return int32(value), true
	case protocol.TypeUint32:
		return uint32(value), true
	case protocol.TypeInt64:
		return int64(value), true
	case protocol.TypeUint64:
		return value, true
	case protocol.TypeFloat16:
		return float16ToFloat32(uint16(value)), true
	case protocol.TypeFloat32:
		return math.Float32frombits(uint32(value)), true
	case protocol.TypeFloat64:
		return math.Float64frombits(value), true
	default:
		return nil, false
	}
}

func toFloat64(v interface{}) float64 {
	switch x := v.(type) {
	case bool:
		if x {
			return 1.0
		}
		return 0.0
	case int8:
		return float64(x)
	case uint8:
		return float64(x)
	case int16:
		return float64(x)
	case uint16:
		return float64(x)
	case int32:
		return float64(x)
	case uint32:
		return float64(x)
	case int64:
		return float64(x)
	case uint64:
		return float64(x)
	case float32:
		return float64(x)
	case float64:
		return x
	}
	return 0
}

func toInt64(v interface{}) int64 {
	switch x := v.(type) {
	case bool:
		if x {
			return 1
		}
		return 0
	case int8:
		return int64(x)
	case uint8:
		return int64(x)
	case int16:
		return int64(x)
	case uint16:
		return int64(x)
	case int32:
		return int64(x)
	case uint32:
		return int64(x)
	case int64:
		return x
	case uint64:
		return int64(x)
	case float32:
		return int64(x)
	case float64:
		return int64(x)
	}
	return 0
}

func toUint64(v interface{}) uint64 {
	switch x := v.(type) {
	case bool:
		if x {
			return 1
		}
		return 0
	case int8:
		return uint64(x)
	case uint8:
		return uint64(x)
	case int16:
		return uint64(x)
	case uint16:
		return uint64(x)
	case int32:
		return uint64(x)
	case uint32:
		return uint64(x)
	case int64:
		return uint64(x)
	case uint64:
		return x
	case float32:
		return uint64(x)
	case float64:
		return uint64(x)
	}
	return 0
}

func toBool(v interface{}) bool {
	switch x := v.(type) {
	case bool:
		return x
	case int8:
		return x != 0
	case uint8:
		return x != 0
	case int16:
		return x != 0
	case uint16:
		return x != 0
	case int32:
		return x != 0
	case uint32:
		return x != 0
	case int64:
		return x != 0
	case uint64:
		return x != 0
	case float32:
		return x != 0
	case float64:
		return x != 0
	}
	return false
}

// formatFloat ensures the float carries a decimal point or exponent.
func formatFloat(f float64) string {
	s := strconv.FormatFloat(f, 'g', -1, 64)
	if !strings.ContainsAny(s, ".eE") {
		s += ".0"
	}
	return s
}

// encodeFieldValue converts a decoded raw value to a line protocol field value.
// Returns "",false to skip (NaN/±Inf).
func encodeFieldValue(raw interface{}, pointType string) (string, bool) {
	switch pointType {
	case "":
		// Follow acquired type (default mapping).
		switch v := raw.(type) {
		case bool:
			return strconv.FormatBool(v), true
		case int8, int16, int32, int64:
			return strconv.FormatInt(toInt64(raw), 10) + "i", true
		case uint8, uint16, uint32, uint64:
			return strconv.FormatUint(toUint64(raw), 10) + "u", true
		case float32, float64:
			f := toFloat64(raw)
			if math.IsNaN(f) || math.IsInf(f, 0) {
				return "", false
			}
			return formatFloat(f), true
		}
		return "", false
	case "float":
		f := toFloat64(raw)
		if math.IsNaN(f) || math.IsInf(f, 0) {
			return "", false
		}
		return formatFloat(f), true
	case "int":
		return strconv.FormatInt(toInt64(raw), 10) + "i", true
	case "uint":
		return strconv.FormatUint(toUint64(raw), 10) + "u", true
	case "bool":
		return strconv.FormatBool(toBool(raw)), true
	}
	return "", false
}

// ──────────────────────────────────────────────
//  Line protocol encoding (§4.4.2)
// ──────────────────────────────────────────────

func escapeMeasurement(s string) string {
	s = strings.ReplaceAll(s, ",", "\\,")
	s = strings.ReplaceAll(s, " ", "\\ ")
	return s
}

func escapeKey(s string) string {
	s = strings.ReplaceAll(s, ",", "\\,")
	s = strings.ReplaceAll(s, "=", "\\=")
	s = strings.ReplaceAll(s, " ", "\\ ")
	return s
}

func resolveField(p influxPoint) string {
	if p.Field != "" {
		return p.Field
	}
	if idx := strings.LastIndex(p.Key, "."); idx >= 0 && idx+1 < len(p.Key) {
		return p.Key[idx+1:]
	}
	return p.Key
}

func convertTimestamp(ts uint64, precision string) uint64 {
	switch precision {
	case "s":
		return ts / 1000
	case "us":
		return ts * 1000
	case "ns":
		return ts * 1000000
	default:
		return ts
	}
}

// encodeLine builds one line protocol line for a block value.
// Returns "",false to skip the point.
func encodeLine(pm *pointMapping, dataType uint8, value uint64, timestamp uint64, precision string) (string, bool) {
	raw, ok := decodeAcquiredValue(dataType, value)
	if !ok {
		return "", false
	}
	fieldVal, ok := encodeFieldValue(raw, pm.pointType)
	if !ok {
		return "", false
	}
	ts := convertTimestamp(timestamp, precision)

	var sb strings.Builder
	sb.WriteString(escapeMeasurement(pm.measurement))

	keys := make([]string, 0, len(pm.tags))
	for k := range pm.tags {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		sb.WriteString(",")
		sb.WriteString(escapeKey(k))
		sb.WriteString("=")
		sb.WriteString(escapeKey(pm.tags[k]))
	}

	sb.WriteString(" ")
	sb.WriteString(escapeKey(pm.field))
	sb.WriteString("=")
	sb.WriteString(fieldVal)
	sb.WriteString(" ")
	sb.WriteString(strconv.FormatUint(ts, 10))

	return sb.String(), true
}

// ──────────────────────────────────────────────
//  HTTP flush (§4.5)
// ──────────────────────────────────────────────

func buildWriteURL(cfg influxInstance) string {
	return fmt.Sprintf("%s/api/v2/write?org=%s&bucket=%s&precision=%s",
		cfg.URL,
		url.QueryEscape(cfg.Org),
		url.QueryEscape(cfg.Bucket),
		cfg.Precision,
	)
}

// doPost sends one write request and classifies the result.
// Returns nil on success, or an error with a category marker.
func (ist *instanceState) doPost(writeURL, body string) error {
	var bodyReader io.Reader
	if *ist.cfg.Gzip == 1 {
		var buf bytes.Buffer
		gz := gzip.NewWriter(&buf)
		gz.Write([]byte(body))
		gz.Close()
		bodyReader = &buf
	} else {
		bodyReader = strings.NewReader(body)
	}

	req, err := http.NewRequest("POST", writeURL, bodyReader)
	if err != nil {
		return fmt.Errorf("build request failed: %v", err)
	}
	req.Header.Set("Content-Type", "text/plain; charset=utf-8")
	req.Header.Set("Authorization", "Token "+ist.cfg.Token)
	if *ist.cfg.Gzip == 1 {
		req.Header.Set("Content-Encoding", "gzip")
	}

	resp, err := ist.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("retryable: %v", err)
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body)

	switch resp.StatusCode {
	case http.StatusNoContent:
		return nil
	case http.StatusBadRequest, http.StatusUnauthorized, http.StatusNotFound:
		return fmt.Errorf("nonretryable-%d", resp.StatusCode)
	case http.StatusRequestEntityTooLarge:
		return fmt.Errorf("retryable-413")
	case http.StatusTooManyRequests:
		return fmt.Errorf("retryable-429")
	default:
		if resp.StatusCode >= 500 {
			return fmt.Errorf("retryable-%d", resp.StatusCode)
		}
		return fmt.Errorf("nonretryable-%d", resp.StatusCode)
	}
}

// flush sends the buffered lines with retry/backoff; on final failure drops them.
func (ist *instanceState) flush() {
	if len(ist.buffer) == 0 {
		return
	}
	body := strings.Join(ist.buffer, "\n") + "\n"
	writeURL := buildWriteURL(ist.cfg)
	retries := *ist.cfg.Retries

	for attempt := 0; ; attempt++ {
		err := ist.doPost(writeURL, body)
		if err == nil {
			ist.buffer = nil
			ist.lastFlush = time.Now()
			return
		}
		if !strings.HasPrefix(err.Error(), "retryable") {
			atomic.AddUint64(&ist.writeErrors, 1)
			log.Printf("instance '%s': non-retryable write error, dropping %d lines: %v", ist.cfg.Name, len(ist.buffer), err)
			ist.buffer = nil
			ist.lastFlush = time.Now()
			return
		}
		if attempt >= retries {
			atomic.AddUint64(&ist.writeErrors, 1)
			log.Printf("instance '%s': write retries exhausted, dropping %d lines: %v", ist.cfg.Name, len(ist.buffer), err)
			ist.buffer = nil
			ist.lastFlush = time.Now()
			return
		}
		backoff := time.Duration(1000*(1<<attempt)) * time.Millisecond
		if backoff > 30*time.Second {
			backoff = 30 * time.Second
		}
		select {
		case <-time.After(backoff):
		case <-ist.quit:
			return
		}
	}
}

// flushOnce sends the buffered lines once (best-effort, no retry), used by stop.
func (ist *instanceState) flushOnce() {
	if len(ist.buffer) == 0 {
		return
	}
	body := strings.Join(ist.buffer, "\n") + "\n"
	writeURL := buildWriteURL(ist.cfg)
	if err := ist.doPost(writeURL, body); err != nil {
		log.Printf("instance '%s': stop flush failed (best-effort), dropping %d lines: %v", ist.cfg.Name, len(ist.buffer), err)
	}
	ist.buffer = nil
}

// ──────────────────────────────────────────────
//  Write loop (§4.2)
// ──────────────────────────────────────────────

func (ist *instanceState) sendRound(shmData []byte) {
	for _, pm := range ist.points {
		dt, ts, val, seq, ok := readBlock(shmData, pm.shmID)
		if !ok {
			continue
		}

		if protocol.VariableTypes[dt] {
			atomic.AddUint64(&ist.itemsSkipped, 1)
			ist.lastSeen[pm.shmID] = seq
			continue
		}

		if seq <= ist.lastSeen[pm.shmID] {
			continue
		}

		line, ok := encodeLine(&pm, dt, val, ts, ist.cfg.Precision)
		if !ok {
			atomic.AddUint64(&ist.itemsSkipped, 1)
			ist.lastSeen[pm.shmID] = seq
			continue
		}

		ist.buffer = append(ist.buffer, line)
		ist.lastSeen[pm.shmID] = seq

		if len(ist.buffer) >= *ist.cfg.BatchSize {
			ist.flush()
		}
	}

	if len(ist.buffer) > 0 && time.Since(ist.lastFlush) >= time.Duration(*ist.cfg.FlushInterval)*time.Millisecond {
		ist.flush()
	}
}

func runInstance(ist *instanceState, shmData []byte) {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("runInstance panic recovered for instance '%s': %v", ist.cfg.Name, r)
		}
	}()
	timer := time.NewTicker(time.Duration(*ist.cfg.Timer) * time.Millisecond)
	defer timer.Stop()

	for {
		select {
		case <-ist.quit:
			return
		case <-timer.C:
			ist.sendRound(shmData)
		}
	}
}

// ──────────────────────────────────────────────
//  MCP tool handlers
// ──────────────────────────────────────────────

func startHandler(ctx context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	if state.started.Load() {
		return newError("ALREADY_RUNNING: start has already been called and service is running, call stop first"), nil
	}

	var args map[string]any
	if err := json.Unmarshal(req.Params.Arguments, &args); err != nil {
		return newError("CONFIG_PATH_MISSING: cannot parse arguments"), nil
	}
	instanceID, _ := args["instance_id"].(string)
	if instanceID == "" {
		return newError("CONFIG_PATH_MISSING: instance_id is required"), nil
	}
	if !validateInstanceID(instanceID) {
		return newError(fmt.Sprintf("INVALID_INSTANCE_ID: instance_id '%s' must match pattern ^c4_[a-zA-Z0-9]+$", instanceID)), nil
	}
	configPath, _ := args["config_path"].(string)
	if configPath == "" {
		return newError("CONFIG_PATH_MISSING: config_path is required"), nil
	}

	instances, err := loadConfig(configPath)
	if err != nil {
		return newError(err.Error()), nil
	}

	if err := validateConfig(instances); err != nil {
		return newError(err.Error()), nil
	}

	shmData, shmFd, err := attachShm(instanceID)
	if err != nil {
		return newError(err.Error()), nil
	}

	var instancesState []*instanceState
	for _, cfg := range instances {
		var pts []pointMapping
		for _, pt := range cfg.Points {
			pts = append(pts, pointMapping{
				shmID:       pt.ShmID,
				measurement: pt.Measurement,
				field:       resolveField(pt),
				pointType:   pt.Type,
				tags:        pt.Tags,
			})
		}
		sort.Slice(pts, func(i, j int) bool { return pts[i].shmID < pts[j].shmID })

		lastSeen := make(map[int]uint64)
		for _, pm := range pts {
			lastSeen[pm.shmID] = 0
		}

		ist := &instanceState{
			cfg:        cfg,
			points:     pts,
			lastSeen:   lastSeen,
			lastFlush:  time.Now(),
			httpClient: &http.Client{Timeout: time.Duration(*cfg.T0) * time.Second},
			quit:       make(chan struct{}),
		}
		instancesState = append(instancesState, ist)
	}

	for _, ist := range instancesState {
		ist.wg.Add(1)
		go func(ist *instanceState) {
			defer ist.wg.Done()
			runInstance(ist, shmData)
		}(ist)
	}

	state.mu.Lock()
	state.instances = instancesState
	state.shmData = shmData
	state.shmFd = shmFd
	state.started.Store(true)
	state.mu.Unlock()

	return newResult("success"), nil
}

func stopHandler(ctx context.Context, req *mcp.CallToolRequest, input struct{}) (*mcp.CallToolResult, any, error) {
	if !state.started.Load() {
		return newResult("success"), nil, nil
	}

	state.mu.Lock()
	defer state.mu.Unlock()

	for _, ist := range state.instances {
		ist.flushOnce()
		close(ist.quit)
		ist.wg.Wait()
	}
	state.instances = nil

	if state.shmData != nil {
		unix.Munmap(state.shmData)
		unix.Close(state.shmFd)
		state.shmData = nil
	}

	state.started.Store(false)

	return newResult("success"), nil, nil
}

// ──────────────────────────────────────────────
//  Helpers
// ──────────────────────────────────────────────

func newResult(text string) *mcp.CallToolResult {
	return &mcp.CallToolResult{
		Content: []mcp.Content{&mcp.TextContent{Text: text}},
	}
}

func newError(text string) *mcp.CallToolResult {
	return &mcp.CallToolResult{
		Content: []mcp.Content{&mcp.TextContent{Text: text}},
		IsError: true,
	}
}

// ──────────────────────────────────────────────
//  Main
// ──────────────────────────────────────────────

func main() {
	server := mcp.NewServer(
		&mcp.Implementation{Name: "c4_influxdb_client", Version: "0.1.0"},
		nil,
	)

	server.AddTool(
		&mcp.Tool{
			Name:        "start",
			Description: "Start InfluxDB writer client instances",
			InputSchema: json.RawMessage(`{"type":"object","properties":{"instance_id":{"type":"string"},"config_path":{"type":"string"}},"required":["instance_id","config_path"]}`),
		},
		startHandler,
	)

	mcp.AddTool(server,
		&mcp.Tool{
			Name:        "stop",
			Description: "Stop all InfluxDB writer instances and release resources",
			InputSchema: json.RawMessage(`{"type":"object","properties":{},"required":[]}`),
		},
		stopHandler,
	)

	if err := server.Run(context.Background(), &mcp.StdioTransport{}); err != nil {
		log.Fatal(err)
	}
}
