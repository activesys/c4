package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"log"
	
	"net"
	"os"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"golang.org/x/sys/unix"

	"c4/mcp/c4_asfp2_client/internal/shm"
)

// ──────────────────────────────────────────────
//  Configuration types
// ──────────────────────────────────────────────

type pointCfg struct {
	Key   string `json:"key"`
	Addr  uint32 `json:"addr"`
	ShmID int    `json:"shm_id"`
}

type instanceCfg struct {
	Name        string     `json:"name"`
	IP          string     `json:"ip"`
	Port        int        `json:"port"`
	T0          int        `json:"t0"`
	T1          int        `json:"t1"`
	T2          int        `json:"t2"`
	Smart       int        `json:"smart"`
	ForwardKack uint8      `json:"forward_kack"`
	InverseKeep uint8      `json:"inverse_keep"`
	Timer       int        `json:"timer"`
	Points      []pointCfg `json:"points"`
}

type clientConfig struct {
	C4ASFP2Client []instanceCfg `json:"c4_asfp2_client"`
}

// ──────────────────────────────────────────────
//  Protocol constants
// ──────────────────────────────────────────────

const (
	// version flag
	flagV211 = "ASFPV211"

	// data types
	asfpTypeBoolean        uint8 = 0
	asfpTypeInt8           uint8 = 1
	asfpTypeUint8          uint8 = 2
	asfpTypeInt16          uint8 = 3
	asfpTypeUint16         uint8 = 4
	asfpTypeInt32          uint8 = 5
	asfpTypeUint32         uint8 = 6
	asfpTypeInt64          uint8 = 7
	asfpTypeUint64         uint8 = 8
	asfpTypeFloat16        uint8 = 9
	asfpTypeFloat32        uint8 = 10
	asfpTypeFloat64        uint8 = 11
	asfpTypeString         uint8 = 12
	asfpTypeBlob           uint8 = 13
	asfpTypeBitstring      uint8 = 14
	asfpTypeBit            uint8 = 15
	asfpTypeLargeDataBlock uint8 = 16

	// attribute bits
	attrKeySequence   = 0x00000001
	attrSameDataType  = 0x00000002
	attrSameTimestamp = 0x00000004
)

var variableTypes = map[uint8]bool{
	asfpTypeString:         true,
	asfpTypeBlob:           true,
	asfpTypeBitstring:      true,
	asfpTypeLargeDataBlock: true,
}

func typeByteSize(t uint8) int {
	switch t {
	case asfpTypeBoolean, asfpTypeBit:
		return 1
	case asfpTypeInt8, asfpTypeUint8:
		return 1
	case asfpTypeInt16, asfpTypeUint16, asfpTypeFloat16:
		return 2
	case asfpTypeInt32, asfpTypeUint32, asfpTypeFloat32:
		return 4
	case asfpTypeInt64, asfpTypeUint64, asfpTypeFloat64:
		return 8
	default:
		return 0
	}
}

// ──────────────────────────────────────────────
//  Float conversion helpers
// ──────────────────────────────────────────────


func float32ToFloat16(bits uint32) uint16 {
	if bits == 0 {
		return 0
	}
	sign := uint32(bits>>31) & 1
	exp := int32((bits >> 23) & 0xFF)
	mant := bits & 0x7FFFFF

	if exp == 0xFF {
		if mant == 0 {
			return uint16(sign<<15) | 0x7C00
		}
		return uint16(sign<<15) | 0x7C00 | uint16(mant>>13)
	}

	exp = exp - 127 + 15
	if exp <= 0 {
		if exp < -10 {
			return uint16(sign << 15)
		}
		mant = (mant | 0x800000) >> uint32(1-exp)
		return uint16(sign<<15) | uint16(mant>>13)
	}
	if exp >= 0x1F {
		return uint16(sign<<15) | 0x7C00
	}

	return uint16(sign<<15) | uint16(exp<<10) | uint16(mant>>13)
}

// ──────────────────────────────────────────────
//  Client state
// ──────────────────────────────────────────────

type instanceStats struct {
	packetsSent  uint64
	itemsSent    uint64
	itemsSkipped uint64
	sendErrors   uint64
	reconnects   uint64
}

type instanceState struct {
	cfg      instanceCfg
	conn     net.Conn
	quit     chan struct{}
	shmIDs   map[int]uint32 // shmID → addr
	lastSeen map[int]uint64 // shmID → last write_seq
	stats    instanceStats
	mu       sync.Mutex // guards conn
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
//  Config loading
// ──────────────────────────────────────────────

func loadConfig(req *mcp.CallToolRequest) ([]instanceCfg, error) {
	rootRes, err := req.Session.ListRoots(context.Background(), nil)
	if err != nil || rootRes == nil || len(rootRes.Roots) == 0 {
		return nil, fmt.Errorf("CONFIG_PATH_MISSING: roots/list protocol call failed, Agent may not be responding")
	}

	configPath := rootRes.Roots[0].URI
	if len(configPath) > 7 && configPath[:7] == "file://" {
		configPath = configPath[7:]
	}

	data, err := os.ReadFile(configPath)
	if err != nil {
		return nil, fmt.Errorf("CONFIG_PATH_MISSING: cannot read config file: %v", err)
	}

	var fullCfg map[string]any
	if err := json.Unmarshal(data, &fullCfg); err != nil {
		return nil, fmt.Errorf("CONFIG_PARSE_ERROR: failed to parse config JSON: %v", err)
	}

	section, ok := fullCfg["c4_asfp2_client"]
	if !ok {
		return nil, fmt.Errorf("CONFIG_PARSE_ERROR: 'c4_asfp2_client' section not found in config")
	}

	rawJSON, _ := json.Marshal(section)
	var instances []instanceCfg
	if err := json.Unmarshal(rawJSON, &instances); err != nil {
		return nil, fmt.Errorf("CONFIG_PARSE_ERROR: failed to parse 'c4_asfp2_client' section: %v", err)
	}

	return instances, nil
}

func validateConfig(instances []instanceCfg) error {
	for _, inst := range instances {
		if inst.IP == "" {
			return fmt.Errorf("CONFIG_PARSE_ERROR: instance '%s' has empty ip field", inst.Name)
		}
		if inst.Port <= 0 || inst.Port > 65535 {
			return fmt.Errorf("CONFIG_PARSE_ERROR: instance '%s' has invalid port %d", inst.Name, inst.Port)
		}
		for _, pt := range inst.Points {
			if pt.Addr > 16777215 {
				return fmt.Errorf("CONFIG_PARSE_ERROR: addr %d exceeds max 16777215", pt.Addr)
			}
			if pt.ShmID == 0 {
				return fmt.Errorf("SHM_ID_NOT_ASSIGNED: point '%s' has shm_id=0, must be assigned by c4_shm_manager first", pt.Key)
			}
		}
	}
	return nil
}

// ──────────────────────────────────────────────
//  Shared memory (O_RDONLY)
// ──────────────────────────────────────────────

func attachShm() ([]byte, int, error) {
	entries, err := os.ReadDir("/dev/shm")
	if err != nil {
		return nil, 0, fmt.Errorf("SHM_OPEN_FAILED: cannot read /dev/shm: %v", err)
	}

	var shmPath string
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), "c4_") {
			shmPath = "/dev/shm/" + e.Name()
			break
		}
	}
	if shmPath == "" {
		return nil, 0, fmt.Errorf("SHM_OPEN_FAILED: no c4_* shared memory found in /dev/shm")
	}

	fd, err := unix.Open(shmPath, unix.O_RDONLY, 0)
	if err != nil {
		return nil, 0, fmt.Errorf("SHM_OPEN_FAILED: shm_open failed for %s: %v", shmPath, err)
	}

	// Read header to get size
	hdrData, err := unix.Mmap(fd, 0, shm.BlockSize, unix.PROT_READ, unix.MAP_SHARED)
	if err != nil {
		unix.Close(fd)
		return nil, 0, fmt.Errorf("SHM_OPEN_FAILED: mmap header failed: %v", err)
	}
	magic := binary.BigEndian.Uint32(hdrData[0:])
	if magic != shm.Magic {
		unix.Munmap(hdrData)
		unix.Close(fd)
		return nil, 0, fmt.Errorf("SHM_CORRUPTED: header magic is invalid (got 0x%08X, expected 0x%08X)", magic, shm.Magic)
	}
	maxPoints := binary.BigEndian.Uint32(hdrData[shm.HdrOffMaxPoints:])
	unix.Munmap(hdrData)

	totalSize := int64(int(maxPoints)+1) * shm.BlockSize
	data, err := unix.Mmap(fd, 0, int(totalSize), unix.PROT_READ, unix.MAP_SHARED)
	if err != nil {
		unix.Close(fd)
		return nil, 0, fmt.Errorf("SHM_OPEN_FAILED: mmap failed: %v", err)
	}

	return data, fd, nil
}

// ──────────────────────────────────────────────
//  Shared memory read (seqlock reader)
// ──────────────────────────────────────────────

// readBlock reads a single block from SHM using seqlock protocol.
// Returns dataType, timestamp, value, writeSeq, ok.
func readBlock(shmData []byte, shmID int) (uint8, uint64, uint64, uint64, bool) {
	off := shmID * shm.BlockSize
	if off+shm.BlockSize > len(shmData) {
		return 0, 0, 0, 0, false
	}

	// Check magic
	magic := binary.BigEndian.Uint32(shmData[off+shm.BlkOffMagic:])
	if magic != shm.Magic {
		return 0, 0, 0, 0, false
	}

	// Check state: block not activated
	if shmData[off+shm.BlkOffState] == 0 {
		return 0, 0, 0, 0, false
	}

	for {
		s1 := binary.BigEndian.Uint64(shmData[off+shm.BlkOffWriteSeq:])
		if s1&1 != 0 {
			// Writer in progress, skip
			return 0, 0, 0, 0, false
		}
		dt := shmData[off+shm.BlkOffType]
		ts := binary.BigEndian.Uint64(shmData[off+shm.BlkOffTimestamp:])
		val := binary.BigEndian.Uint64(shmData[off+shm.BlkOffValue:])
		s2 := binary.BigEndian.Uint64(shmData[off+shm.BlkOffWriteSeq:])
		if s1 == s2 {
			return dt, ts, val, s1, true
		}
		// retry
	}
}

// ──────────────────────────────────────────────
//  Encode ASFPV211 packet
// ──────────────────────────────────────────────

// shmItem represents a data item read from shared memory, ready for encoding.
type shmItem struct {
	addr      uint32
	dataType  uint8
	timestamp uint64
	value     uint64
}

// encodeASFPV211 builds a single ASFPV211 data packet for a subgroup of items.
// The subgroup is guaranteed to have consecutive addrs (KEY_SEQUENCE=1).
// Returns the encoded byte slice.
func encodeASFPV211(items []shmItem, smart int) []byte {
	count := len(items)
	if count == 0 {
		return nil
	}

	// Apply smart timestamp zeroing
	for i := range items {
		if smart == 1 {
			items[i].timestamp = (items[i].timestamp / 1000) * 1000
		}
	}

	// Detect attribute flags for this subgroup
	var attr uint32 = attrKeySequence // guaranteed by subgrouping

	// SAME_DATA_TYPE: all types identical
	sameDataType := true
	for i := 1; i < count; i++ {
		if items[i].dataType != items[0].dataType {
			sameDataType = false
			break
		}
	}
	if sameDataType {
		attr |= attrSameDataType
	}

	// SAME_TIMESTAMP: all timestamps identical (after smart zeroing)
	sameTimestamp := true
	for i := 1; i < count; i++ {
		if items[i].timestamp != items[0].timestamp {
			sameTimestamp = false
			break
		}
	}
	if sameTimestamp {
		attr |= attrSameTimestamp
	}

	hasKey := (attr & attrKeySequence) != 0
	hasType := (attr & attrSameDataType) != 0
	hasTs := (attr & attrSameTimestamp) != 0

	// Calculate sizes
	mutableSize := 0
	if hasType {
		mutableSize += 1
	}
	if hasKey {
		mutableSize += 3
	}
	if hasTs {
		mutableSize += 8
	}

	dataType := items[0].dataType

	// Check BIT compression: all 3 flags on + type is BOOLEAN or BIT
	bitCompression := hasKey && hasType && hasTs && (dataType == asfpTypeBoolean || dataType == asfpTypeBit)

	var dataSize int
	if bitCompression {
		dataSize = (count + 7) / 8
	} else {
		for i := 0; i < count; i++ {
			if !hasType {
				dataSize += 1 // type
			}
			if !hasKey {
				dataSize += 3 // key
			}
			if !hasTs {
				dataSize += 8 // timestamp
			}
			dataSize += typeByteSize(items[i].dataType)
		}
	}

	totalLength := 16 + mutableSize + dataSize
	lengthLow := totalLength & 0xFFFF
	attrHigh := (totalLength >> 16) & 0xFFFF

	buf := make([]byte, totalLength)

	// ── Header (16 bytes) ──
	copy(buf[0:8], flagV211)
	binary.BigEndian.PutUint16(buf[8:10], uint16(lengthLow))
	binary.BigEndian.PutUint16(buf[10:12], uint16(count))
	// Attribute field at offset 12: (attrHigh << 16) | attr (v2.1.x format)
	binary.BigEndian.PutUint32(buf[12:16], uint32(attrHigh)<<16|attr)

	pos := 16

	// ── Mutable (variable) ──
	// Order matches server decoder: type, key, timestamp
	if hasType {
		buf[pos] = items[0].dataType
		pos++
	}
	if hasKey {
		firstKey := items[0].addr
		buf[pos] = byte((firstKey >> 16) & 0xFF)
		buf[pos+1] = byte((firstKey >> 8) & 0xFF)
		buf[pos+2] = byte(firstKey & 0xFF)
		pos += 3
	}
	if hasTs {
		binary.BigEndian.PutUint64(buf[pos:pos+8], items[0].timestamp)
		pos += 8
	}

	// ── Data ──
	if bitCompression {
		for i := 0; i < count; i++ {
			byteIdx := i / 8
			bitIdx := i % 8
			if items[i].value&1 != 0 {
				buf[pos+byteIdx] |= 1 << bitIdx
			}
		}
		// Remaining bits already zero from make()
	} else {
		for i := 0; i < count; i++ {
			item := items[i]

			if !hasType {
				buf[pos] = item.dataType
				pos++
			}
			if !hasKey {
				buf[pos] = byte((item.addr >> 16) & 0xFF)
				buf[pos+1] = byte((item.addr >> 8) & 0xFF)
				buf[pos+2] = byte(item.addr & 0xFF)
				pos += 3
			}
			if !hasTs {
				binary.BigEndian.PutUint64(buf[pos:pos+8], item.timestamp)
				pos += 8
			}

			// Value encoding (network/big-endian for all types)
			valueSize := typeByteSize(item.dataType)
			switch item.dataType {
			case asfpTypeBoolean, asfpTypeBit:
				buf[pos] = byte(item.value & 1)
			case asfpTypeInt8:
				buf[pos] = byte(int8(item.value))
			case asfpTypeUint8:
				buf[pos] = byte(item.value)
			case asfpTypeInt16:
				binary.BigEndian.PutUint16(buf[pos:pos+2], uint16(int16(item.value)))
			case asfpTypeUint16:
				binary.BigEndian.PutUint16(buf[pos:pos+2], uint16(item.value))
			case asfpTypeFloat16:
				// SHM stores float16 as float32 bit pattern; convert back to float16
				f16 := float32ToFloat16(uint32(item.value))
				binary.BigEndian.PutUint16(buf[pos:pos+2], f16)
			case asfpTypeInt32:
				binary.BigEndian.PutUint32(buf[pos:pos+4], uint32(int32(item.value)))
			case asfpTypeUint32:
				binary.BigEndian.PutUint32(buf[pos:pos+4], uint32(item.value))
			case asfpTypeFloat32:
				// SHM stores float32 bit pattern in native byte order as uint64.
				// The uint32 numeric value contains the IEEE 754 bits in canonical form;
				// binary.BigEndian.PutUint32 writes them in BE (network) order — correct for v211.
				binary.BigEndian.PutUint32(buf[pos:pos+4], uint32(item.value))
			case asfpTypeInt64, asfpTypeUint64:
				binary.BigEndian.PutUint64(buf[pos:pos+8], item.value)
			case asfpTypeFloat64:
				binary.BigEndian.PutUint64(buf[pos:pos+8], item.value)
			}
			pos += valueSize
		}
	}

	return buf
}

// ──────────────────────────────────────────────
//  Send loop
// ──────────────────────────────────────────────

func sendRound(ist *instanceState, shmData []byte) {
	// 1. Scan all configured shm_ids → read blocks via seqlock
	var items []shmItem
	for shmID, addr := range ist.shmIDs {
		dt, ts, val, seq, ok := readBlock(shmData, shmID)
		if !ok {
			continue
		}

		// Skip non-numeric types
		if variableTypes[dt] {
			atomic.AddUint64(&ist.stats.itemsSkipped, 1)
			continue
		}

		// Only send if write_seq > last_seen
		lastSeq := ist.lastSeen[shmID]
		if seq <= lastSeq {
			continue
		}

		items = append(items, shmItem{
			addr:      addr,
			dataType:  dt,
			timestamp: ts,
			value:     val,
		})
	}

	if len(items) == 0 {
		return
	}

	// 2. Sort by addr ascending
	sort.Slice(items, func(i, j int) bool {
		return items[i].addr < items[j].addr
	})

	// 3. Split by addr continuity into subgroups
	var subgroups [][]shmItem
	current := []shmItem{items[0]}
	for i := 1; i < len(items); i++ {
		if items[i].addr == items[i-1].addr+1 {
			current = append(current, items[i])
		} else {
			subgroups = append(subgroups, current)
			current = []shmItem{items[i]}
		}
	}
	subgroups = append(subgroups, current)

	// 4. Encode and send each subgroup
	ist.mu.Lock()
	conn := ist.conn
	ist.mu.Unlock()

	if conn == nil {
		return
	}

	for _, sg := range subgroups {
		pkt := encodeASFPV211(sg, ist.cfg.Smart)
		if pkt == nil {
			continue
		}

		_, err := conn.Write(pkt)
		if err != nil {
			atomic.AddUint64(&ist.stats.sendErrors, 1)
			// Connection broken — close and attempt reconnect
			ist.mu.Lock()
			if ist.conn != nil {
				ist.conn.Close()
				ist.conn = nil
			}
			ist.mu.Unlock()
			// Try reconnect
			newConn, dialErr := net.Dial("tcp", fmt.Sprintf("%s:%d", ist.cfg.IP, ist.cfg.Port))
			if dialErr == nil {
				ist.mu.Lock()
				ist.conn = newConn
				ist.mu.Unlock()
				atomic.AddUint64(&ist.stats.reconnects, 1)
			}
			// If reconnect failed, skip remaining subgroups this round
			return
		}
		atomic.AddUint64(&ist.stats.packetsSent, 1)
		atomic.AddUint64(&ist.stats.itemsSent, uint64(len(sg)))
	}

	// 5. Update last_seen
	for _, item := range items {
		// Find shmID for this addr
		for shmID, addr := range ist.shmIDs {
			if addr == item.addr {
				ist.lastSeen[shmID] = binary.BigEndian.Uint64(shmData[shmID*shm.BlockSize+shm.BlkOffWriteSeq:])
				break
			}
		}
	}
}

func runSender(ist *instanceState, shmData []byte) {
	timer := time.NewTicker(time.Duration(ist.cfg.Timer) * time.Millisecond)
	defer timer.Stop()

	for {
		select {
		case <-ist.quit:
			return
		case <-timer.C:
			sendRound(ist, shmData)
		}
	}
}

// ──────────────────────────────────────────────
//  MCP Tool Handlers
// ──────────────────────────────────────────────

func startHandler(ctx context.Context, req *mcp.CallToolRequest, input struct{}) (*mcp.CallToolResult, any, error) {
	if state.started.Load() {
		return newError("ALREADY_RUNNING: start has already been called and service is running, call stop first"), nil, nil
	}

	instances, err := loadConfig(req)
	if err != nil {
		return newError(err.Error()), nil, nil
	}

	if err := validateConfig(instances); err != nil {
		return newError(err.Error()), nil, nil
	}

	// Empty instances array is valid — start succeeds with no senders
	if len(instances) == 0 {
		state.started.Store(true)
		return newResult("success"), nil, nil
	}

	shmData, shmFd, err := attachShm()
	if err != nil {
		return newError(err.Error()), nil, nil
	}

	state.mu.Lock()
	state.shmData = shmData
	state.shmFd = shmFd

	var instancesState []*instanceState
	var lastErr string

	for _, cfg := range instances {
		// Build shmID → addr map
		shmIDs := make(map[int]uint32)
		lastSeen := make(map[int]uint64)
		for _, pt := range cfg.Points {
			if pt.ShmID > 0 {
				shmIDs[pt.ShmID] = pt.Addr
				lastSeen[pt.ShmID] = 0
			}
		}

		// Verify all shm_ids are within range
		for shmID := range shmIDs {
			off := shmID * shm.BlockSize
			if off+shm.BlockSize > len(shmData) {
				lastErr = fmt.Sprintf("SHM_ID_NOT_ASSIGNED: instance '%s': shm_id %d exceeds shared memory range", cfg.Name, shmID)
				break
			}
		}
		if lastErr != "" {
			break
		}

		// Connect to target
		addr := fmt.Sprintf("%s:%d", cfg.IP, cfg.Port)
		conn, err := net.Dial("tcp", addr)
		if err != nil {
			lastErr = fmt.Sprintf("CONNECT_FAILED: connect to %s failed: %v", addr, err)
			break
		}

		ist := &instanceState{
			cfg:      cfg,
			conn:     conn,
			quit:     make(chan struct{}),
			shmIDs:   shmIDs,
			lastSeen: lastSeen,
		}
		instancesState = append(instancesState, ist)
	}

	if lastErr != "" {
		// Tear down: close all opened connections
		for _, ist := range instancesState {
			ist.conn.Close()
		}
		unix.Munmap(shmData)
		unix.Close(shmFd)
		state.mu.Unlock()
		return newError(lastErr), nil, nil
	}

	// Start goroutines
	for _, ist := range instancesState {
		go runSender(ist, shmData)
	}

	state.instances = instancesState
	state.started.Store(true)
	state.mu.Unlock()

	return newResult("success"), nil, nil
}

func stopHandler(ctx context.Context, req *mcp.CallToolRequest, input struct{}) (*mcp.CallToolResult, any, error) {
	if !state.started.Load() {
		return newError("SERVICE_NOT_READY: start has not been called"), nil, nil
	}

	state.mu.Lock()
	defer state.mu.Unlock()

	for _, ist := range state.instances {
		close(ist.quit)
		ist.mu.Lock()
		if ist.conn != nil {
			ist.conn.Close()
		}
		ist.mu.Unlock()
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

func statusHandler(ctx context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	if !state.started.Load() {
		return newError("SERVICE_NOT_READY: start has not been called"), nil
	}

	type instStatus struct {
		Name        string `json:"name"`
		Target      string `json:"target"`
		State       string `json:"state"`
		PointsCount int    `json:"points_count"`
		Stats       struct {
			PacketsSent  uint64 `json:"packets_sent"`
			ItemsSent    uint64 `json:"items_sent"`
			ItemsSkipped uint64 `json:"items_skipped"`
			SendErrors   uint64 `json:"send_errors"`
			Reconnects   uint64 `json:"reconnects"`
		} `json:"stats"`
	}

	state.mu.Lock()
	defer state.mu.Unlock()

	var result []instStatus
	for _, ist := range state.instances {
		s := instStatus{
			Name:        ist.cfg.Name,
			Target:      fmt.Sprintf("%s:%d", ist.cfg.IP, ist.cfg.Port),
			PointsCount: len(ist.cfg.Points),
		}

		// Determine state
		ist.mu.Lock()
		if ist.conn != nil {
			s.State = "running"
		} else {
			s.State = "disconnected"
		}
		ist.mu.Unlock()

		s.Stats.PacketsSent = atomic.LoadUint64(&ist.stats.packetsSent)
		s.Stats.ItemsSent = atomic.LoadUint64(&ist.stats.itemsSent)
		s.Stats.ItemsSkipped = atomic.LoadUint64(&ist.stats.itemsSkipped)
		s.Stats.SendErrors = atomic.LoadUint64(&ist.stats.sendErrors)
		s.Stats.Reconnects = atomic.LoadUint64(&ist.stats.reconnects)
		result = append(result, s)
	}

	jsonData, _ := json.Marshal(map[string]any{"instances": result})
	return newResult(string(jsonData)), nil
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
		&mcp.Implementation{Name: "c4_asfp2_client", Version: "0.1.0"},
		nil,
	)

	mcp.AddTool(server,
		&mcp.Tool{Name: "start", Description: "Start ASFP2 client sender instances"},
		startHandler,
	)

	mcp.AddTool(server,
		&mcp.Tool{
			Name:        "stop",
			Description: "Stop all ASFP2 client instances and release resources",
			InputSchema: json.RawMessage(`{"type":"object","properties":{},"required":[]}`),
		},
		stopHandler,
	)

	server.AddTool(
		&mcp.Tool{
			Name:        "status",
			Description: "Query per-instance runtime status and statistics",
			InputSchema: json.RawMessage(`{"type":"object","properties":{},"required":[]}`),
		},
		statusHandler,
	)

	if err := server.Run(context.Background(), &mcp.StdioTransport{}); err != nil {
		log.Fatal(err)
	}
}
