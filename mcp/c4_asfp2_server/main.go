package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"log"
	"math"
	"net"
	"os"
	"strings"
	"sync"
	"sync/atomic"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"golang.org/x/sys/unix"

	"c4/mcp/c4_asfp2_server/internal/shm"
)

// ──────────────────────────────────────────────
//  Configuration types
// ──────────────────────────────────────────────

type pointCfg struct {
	ID    string `json:"id"`
	Addr  uint32 `json:"addr"`
	ShmID int    `json:"shm_id"`
}

type instanceCfg struct {
	Name        string     `json:"name"`
	ID          string     `json:"id"`
	Port        int        `json:"port"`
	T1          int        `json:"t1"`
	T2          int        `json:"t2"`
	ForwardKack uint8      `json:"forward_kack"`
	InverseKeep uint8      `json:"inverse_keep"`
	Points      []pointCfg `json:"points"`
}

type serverConfig struct {
	C4ASFP2Server []instanceCfg `json:"c4_asfp2_server"`
}

// ──────────────────────────────────────────────
//  Protocol constants
// ──────────────────────────────────────────────

const (
	// version flags
	flagV200 = "ASFPV200"
	flagV210 = "ASFPV210"
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
	attrKeySequence     = 0x00000001
	attrSameDataType    = 0x00000002
	attrSameTimestamp   = 0x00000004
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
//  Server state
// ──────────────────────────────────────────────

type instanceStats struct {
	packetsReceived uint64
	itemsReceived   uint64
	itemsWritten    uint64
	itemsDropped    uint64
	parseErrors     uint64
}

type instanceState struct {
	cfg       instanceCfg
	listener  net.Listener
	addrMap   map[uint32]int // addr → shm_id
	stats     instanceStats
	paused    atomic.Bool
	quit      chan struct{}
}

type serverState struct {
	started    atomic.Bool
	instances  []*instanceState
	mu         sync.Mutex
	shmData    []byte
	shmFd      int
}

var state = &serverState{}

// ──────────────────────────────────────────────
//  Config loading
// ──────────────────────────────────────────────

func loadConfig(req *mcp.CallToolRequest) ([]instanceCfg, string, error) {
	rootRes, err := req.Session.ListRoots(context.Background(), nil)
	if err != nil || rootRes == nil || len(rootRes.Roots) == 0 {
		return nil, "", fmt.Errorf("CONFIG_PATH_MISSING: roots/list protocol call failed, Agent may not be responding")
	}

	configPath := rootRes.Roots[0].URI
	if len(configPath) > 7 && configPath[:7] == "file://" {
		configPath = configPath[7:]
	}

	data, err := os.ReadFile(configPath)
	if err != nil {
		return nil, "", fmt.Errorf("CONFIG_PATH_MISSING: cannot read config file: %v", err)
	}

	var fullCfg map[string]any
	if err := json.Unmarshal(data, &fullCfg); err != nil {
		return nil, "", fmt.Errorf("CONFIG_PARSE_ERROR: failed to parse config JSON: %v", err)
	}

	section, ok := fullCfg["c4_asfp2_server"]
	if !ok {
		return nil, "", fmt.Errorf("CONFIG_PARSE_ERROR: 'c4_asfp2_server' section not found in config")
	}

	rawJSON, _ := json.Marshal(section)
	var instances []instanceCfg
	if err := json.Unmarshal(rawJSON, &instances); err != nil {
		return nil, "", fmt.Errorf("CONFIG_PARSE_ERROR: failed to parse 'c4_asfp2_server' section: %v", err)
	}

	return instances, configPath, nil
}

func validateConfig(instances []instanceCfg) error {
	ports := make(map[int]string)
	for _, inst := range instances {
		if inst.Port <= 0 || inst.Port > 65535 {
			return fmt.Errorf("CONFIG_PARSE_ERROR: instance '%s' has invalid port %d", inst.ID, inst.Port)
		}
		if existing, ok := ports[inst.Port]; ok {
			return fmt.Errorf("PORT_CONFLICT: port %d duplicated across instances '%s' and '%s'", inst.Port, existing, inst.ID)
		}
		ports[inst.Port] = inst.ID
		if inst.ID == "" {
			return fmt.Errorf("CONFIG_PARSE_ERROR: instance has empty id field")
		}
		for _, pt := range inst.Points {
			if pt.Addr > 16777215 {
				return fmt.Errorf("CONFIG_PARSE_ERROR: addr %d exceeds max 16777215", pt.Addr)
			}
		}
	}
	return nil
}

// ──────────────────────────────────────────────
//  Shared memory
// ──────────────────────────────────────────────

func attachShm(configPath string) ([]byte, int, error) {
	// Find shm instance ID from config
	data, err := os.ReadFile(configPath)
	if err != nil {
		return nil, 0, fmt.Errorf("SHM_OPEN_FAILED: cannot read config: %v", err)
	}

	var fullCfg map[string]any
	json.Unmarshal(data, &fullCfg)

	// find shm path: /dev/shm/c4_{id}
	// We scan /dev/shm for c4_* files
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

	fd, err := unix.Open(shmPath, unix.O_RDWR, 0)
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
	data, err = unix.Mmap(fd, 0, int(totalSize), unix.PROT_READ|unix.PROT_WRITE, unix.MAP_SHARED)
	if err != nil {
		unix.Close(fd)
		return nil, 0, fmt.Errorf("SHM_OPEN_FAILED: mmap failed: %v", err)
	}

	return data, fd, nil
}

// ──────────────────────────────────────────────
//  ASFP2 Parser
// ──────────────────────────────────────────────

func parseASFP2Data(conn net.Conn, inst *instanceState, shmData []byte) {
	defer conn.Close()

	buf := make([]byte, 65536)
	for {
		n, err := conn.Read(buf)
		if err != nil {
			return
		}
		if n < 1 {
			continue
		}

		pos := 0
		for pos < n {
			firstByte := buf[pos]

			// Heartbeat: 'K' prefix
			if firstByte == 'K' {
				if pos+4 <= n && string(buf[pos:pos+4]) == "KEEP" {
					// Forward KA: reply with forward_kack
					conn.Write([]byte{inst.cfg.ForwardKack})
					pos += 4
					continue
				}
				if pos+4 <= n && string(buf[pos:pos+4]) == "KACK" {
					// Reverse KA Ack — acknowledge
					pos += 4
					continue
				}
				// Unknown, skip
				pos++
				continue
			}

			// Data packet: must start with 'A'
			if firstByte != 'A' {
				pos++
				atomic.AddUint64(&inst.stats.parseErrors, 1)
				continue
			}

			// Need at least 8B flag
			if pos+8 > n {
				break
			}
			pktStart := pos
			flag := string(buf[pos : pos+8])
			var versionStr string
			switch flag {
			case flagV200:
				versionStr = flagV200
			case flagV210:
				versionStr = flagV210
			case flagV211:
				versionStr = flagV211
			default:
				pos++
				atomic.AddUint64(&inst.stats.parseErrors, 1)
				continue
			}
			pos += 8

			// Parse header: Length + Count + Attribute
			if pos+8 > n {
				break
			}

			var length, count int
			var attribute uint32

			if versionStr == flagV200 {
				length = int(binary.BigEndian.Uint16(buf[pos : pos+2]))
				count = int(binary.BigEndian.Uint16(buf[pos+2 : pos+4]))
				attribute = binary.BigEndian.Uint32(buf[pos+4 : pos+8])
			} else {
				// v2.1.0/v2.1.1: Length 4B (high 2B from attribute slot)
				lengthLow := int(binary.BigEndian.Uint16(buf[pos : pos+2]))
				lengthExt := int(binary.BigEndian.Uint16(buf[pos+4 : pos+6]))
				length = lengthLow | (lengthExt << 16)
				count = int(binary.BigEndian.Uint16(buf[pos+2 : pos+4]))
				attribute = binary.BigEndian.Uint32(buf[pos+4 : pos+8])
				// Fix attribute: high 2B already extracted for length
				attribute = (attribute & 0x0000FFFF) | (uint32(lengthExt) << 16)
			}
			pos += 8

			if length < 16 || pktStart+length > n {
				break
			}

			// Parse Mutable
			var mutableKey uint32
			var mutableType uint8
			var mutableTimestamp uint64
			hasKey := (attribute & attrKeySequence) != 0
			hasType := (attribute & attrSameDataType) != 0
			hasTs := (attribute & attrSameTimestamp) != 0

			if hasKey && hasType && hasTs {
				// Mutable order: type(1B), key(3B), timestamp(8B)
			}
			if hasType {
				mutableType = buf[pos]
				pos++
			}
			if hasKey {
				mutableKey = uint32(buf[pos])<<16 | uint32(buf[pos+1])<<8 | uint32(buf[pos+2])
				pos += 3
			}
			if hasTs {
				mutableTimestamp = binary.BigEndian.Uint64(buf[pos : pos+8])
				pos += 8
			}

			// Drop entire packet if same data type is variable-length
			if hasType && variableTypes[mutableType] {
				atomic.AddUint64(&inst.stats.parseErrors, 1)
				pos = pos - 8 - len(flag) - 8 + length // skip to end of this packet
				continue
			}

			// BIT compression mode
			if hasKey && hasType && hasTs && (mutableType == asfpTypeBoolean || mutableType == asfpTypeBit) {
				compressedBytes := (count + 7) / 8
				if pos+compressedBytes > n {
					break
				}
				for i := 0; i < count; i++ {
					byteIdx := i / 8
					bitIdx := i % 8
					bit := (buf[pos+byteIdx] >> bitIdx) & 1
					addr := mutableKey + uint32(i)
					shmID, ok := inst.addrMap[addr]
					if ok && !inst.paused.Load() {
						writeBlock(shmData, shmID, mutableType, mutableTimestamp, uint64(bit), 1)
						atomic.AddUint64(&inst.stats.itemsWritten, 1)
					} else {
						atomic.AddUint64(&inst.stats.itemsDropped, 1)
					}
					atomic.AddUint64(&inst.stats.itemsReceived, 1)
				}
				pos += compressedBytes
				atomic.AddUint64(&inst.stats.packetsReceived, 1)
				continue
			}

			// Parse Data items
			for i := 0; i < count; i++ {
				itemType := mutableType
				itemKey := mutableKey + uint32(i)
				itemTs := mutableTimestamp

				if !hasType {
					if pos >= n {
						break
					}
					itemType = buf[pos]
					pos++
				}
				if !hasKey {
					if pos+3 > n {
						break
					}
					itemKey = uint32(buf[pos])<<16 | uint32(buf[pos+1])<<8 | uint32(buf[pos+2])
					pos += 3
				}
				if !hasTs {
					if pos+8 > n {
						break
					}
					itemTs = binary.BigEndian.Uint64(buf[pos : pos+8])
					pos += 8
				}

				atomic.AddUint64(&inst.stats.itemsReceived, 1)

				// Skip variable-length items
				if variableTypes[itemType] {
					atomic.AddUint64(&inst.stats.itemsDropped, 1)
					continue
				}

				valueSize := typeByteSize(itemType)
				if pos+valueSize > n {
					break
				}

				var value uint64
				swapFlt := versionStr == flagV211 && (itemType == asfpTypeFloat16 || itemType == asfpTypeFloat32 || itemType == asfpTypeFloat64)

				switch itemType {
				case asfpTypeBoolean, asfpTypeBit:
					value = uint64(buf[pos] & 1)
				case asfpTypeInt8:
					v := int8(buf[pos])
					value = uint64(int64(v))
				case asfpTypeUint8:
					value = uint64(buf[pos])
				case asfpTypeInt16:
					v := int16(binary.BigEndian.Uint16(buf[pos : pos+2]))
					value = uint64(int64(v))
				case asfpTypeUint16:
					value = uint64(binary.BigEndian.Uint16(buf[pos : pos+2]))
				case asfpTypeInt32:
					v := int32(binary.BigEndian.Uint32(buf[pos : pos+4]))
					value = uint64(int64(v))
				case asfpTypeUint32:
					value = uint64(binary.BigEndian.Uint32(buf[pos : pos+4]))
				case asfpTypeInt64, asfpTypeUint64:
					value = binary.BigEndian.Uint64(buf[pos : pos+8])
				case asfpTypeFloat16:
					bits := binary.BigEndian.Uint16(buf[pos : pos+2])
					if swapFlt {
						bits = bits<<8 | bits>>8 // swap BE to LE for native shm storage
					}
					value = uint64(float16ToFloat32(bits))
				case asfpTypeFloat32:
					bits := binary.BigEndian.Uint32(buf[pos : pos+4])
					if swapFlt {
						bits = bits>>24 | (bits>>8)&0xFF00 | (bits<<8)&0xFF0000 | bits<<24
					}
					value = uint64(math.Float32bits(math.Float32frombits(bits)))
				case asfpTypeFloat64:
					bits := binary.BigEndian.Uint64(buf[pos : pos+8])
					if swapFlt {
						bits = bits>>56 | (bits>>40)&0xFF00 | (bits>>24)&0xFF0000 | (bits>>8)&0xFF000000 |
							(bits<<8)&0xFF00000000 | (bits<<24)&0xFF0000000000 | (bits<<40)&0xFF000000000000 | bits<<56
					}
					value = math.Float64bits(math.Float64frombits(bits))
				}
				pos += valueSize

				shmID, ok := inst.addrMap[itemKey]
				if ok && !inst.paused.Load() {
					writeBlock(shmData, shmID, itemType, itemTs, value, valueSize)
					atomic.AddUint64(&inst.stats.itemsWritten, 1)
				} else {
					atomic.AddUint64(&inst.stats.itemsDropped, 1)
					if !ok {
					}
				}
			}
			atomic.AddUint64(&inst.stats.packetsReceived, 1)
		}
	}
}

// float16ToFloat32 converts IEEE 754 half-precision to float32
func float16ToFloat32(h uint16) uint32 {
	s := uint32(h>>15) & 1
	e := uint32(h>>10) & 0x1F
	m := uint32(h) & 0x3FF

	if e == 0 {
		if m == 0 {
			return s << 31
		}
		// Subnormal
		for (m & 0x400) == 0 {
			m <<= 1
			e++
		}
		m &= 0x3FF
		e = 1
	} else if e == 0x1F {
		return (s << 31) | (0xFF << 23) | (m << 13)
	}

	e = e - 15 + 127
	return (s << 31) | (e << 23) | (m << 13)
}

// ──────────────────────────────────────────────
//  Shared memory write (seqlock)
// ──────────────────────────────────────────────

func writeBlock(shmData []byte, shmID int, dataType uint8, timestamp uint64, value uint64, valueSize int) {
	off := shmID * shm.BlockSize
	if off+shm.BlockSize > len(shmData) {
		return
	}

	// Verify magic
	magic := binary.BigEndian.Uint32(shmData[off+shm.BlkOffMagic:])
	if magic != shm.Magic {
		return
	}

	// Activate block on first write
	if shmData[off+shm.BlkOffState] == 0 {
		shmData[off+shm.BlkOffState] = 1
		binary.BigEndian.PutUint64(shmData[off+shm.BlkOffWriteSeq:], 0)
	}

	// Seqlock: increment to odd
	writeSeq := binary.BigEndian.Uint64(shmData[off+shm.BlkOffWriteSeq:])
	binary.BigEndian.PutUint64(shmData[off+shm.BlkOffWriteSeq:], writeSeq+1)

	// Write data
	binary.BigEndian.PutUint64(shmData[off+shm.BlkOffTimestamp:], timestamp)
	shmData[off+shm.BlkOffType] = dataType
	binary.BigEndian.PutUint64(shmData[off+shm.BlkOffValue:], value)

	// Seqlock: increment to even
	binary.BigEndian.PutUint64(shmData[off+shm.BlkOffWriteSeq:], writeSeq+2)
}

// ──────────────────────────────────────────────
//  MCP Tool Handlers
// ──────────────────────────────────────────────

func startHandler(ctx context.Context, req *mcp.CallToolRequest, input struct{}) (*mcp.CallToolResult, any, error) {
	if state.started.Load() {
		return newError("ALREADY_STARTED: start has already been called, service is running"), nil, nil
	}

	instances, configPath, err := loadConfig(req)
	if err != nil {
		return newError(err.Error()), nil, nil
	}

	if err := validateConfig(instances); err != nil {
		return newError(err.Error()), nil, nil
	}

	// Empty instances array is valid — start succeeds with no port listeners
	if len(instances) == 0 {
		state.started.Store(true)
		return newResult("success"), nil, nil
	}

	shmData, shmFd, err := attachShm(configPath)
	if err != nil {
		return newError(err.Error()), nil, nil
	}

	state.mu.Lock()
	state.shmData = shmData
	state.shmFd = shmFd

	var instancesState []*instanceState
	var lastErr string

	for _, cfg := range instances {
		addrMap := make(map[uint32]int)
		for _, pt := range cfg.Points {
			if pt.ShmID > 0 {
				addrMap[pt.Addr] = pt.ShmID
			}
		}

		listener, err := net.Listen("tcp", fmt.Sprintf(":%d", cfg.Port))
		if err != nil {
			lastErr = fmt.Sprintf("PORT_BIND_FAILED: instance '%s': cannot bind port %d: %v", cfg.ID, cfg.Port, err)
			break
		}

		ist := &instanceState{
			cfg:      cfg,
			listener: listener,
			addrMap:  addrMap,
			quit:     make(chan struct{}),
		}
		instancesState = append(instancesState, ist)
	}

	if lastErr != "" {
		for _, ist := range instancesState {
			ist.listener.Close()
		}
		state.mu.Unlock()
		return newError(lastErr), nil, nil
	}

	// Start goroutines
	for _, ist := range instancesState {
		go runServer(ist, shmData)
	}

	state.instances = instancesState
	state.started.Store(true)
	state.mu.Unlock()

	return newResult("success"), nil, nil
}

func runServer(ist *instanceState, shmData []byte) {
	defer ist.listener.Close()

	for {
		conn, err := ist.listener.Accept()
		if err != nil {
			select {
			case <-ist.quit:
				return
			default:
			}
			continue
		}
		go parseASFP2Data(conn, ist, shmData)
	}
}

func pauseHandler(ctx context.Context, req *mcp.CallToolRequest, input struct {
	ShmIDs []int `json:"shm_ids"`
}) (*mcp.CallToolResult, any, error) {
	if !state.started.Load() {
		return newError("SERVICE_NOT_READY: start has not been called"), nil, nil
	}

	state.mu.Lock()
	defer state.mu.Unlock()

	pauseAll := len(input.ShmIDs) == 0
	targetSet := make(map[int]bool)
	for _, id := range input.ShmIDs {
		targetSet[id] = true
	}

	for _, ist := range state.instances {
		if pauseAll {
			ist.paused.Store(true)
		} else {
			// Check if any point in this instance's addrMap has matching shm_id
			for _, shmID := range ist.addrMap {
				if targetSet[shmID] {
					ist.paused.Store(true)
					break
				}
			}
		}
	}

	return newResult("success"), nil, nil
}

func resumeHandler(ctx context.Context, req *mcp.CallToolRequest, input struct {
	ShmIDs       []int `json:"shm_ids"`
	NewMaxPoints int   `json:"new_max_points"`
}) (*mcp.CallToolResult, any, error) {
	if !state.started.Load() {
		return newError("SERVICE_NOT_READY: start has not been called"), nil, nil
	}

	state.mu.Lock()
	defer state.mu.Unlock()

	// Reload config via roots/list
	instances, configPath, err := loadConfig(req)
	if err != nil {
		return newError(err.Error()), nil, nil
	}

	// Remap shm
	if state.shmData != nil {
		unix.Munmap(state.shmData)
		unix.Close(state.shmFd)
	}
	shmData, shmFd, err := attachShm(configPath)
	if err != nil {
		return newError(err.Error()), nil, nil
	}
	state.shmData = shmData
	state.shmFd = shmFd

	// Rebuild instances
	for _, ist := range state.instances {
		close(ist.quit)
		ist.listener.Close()
	}

	var newInstances []*instanceState
	for _, cfg := range instances {
		addrMap := make(map[uint32]int)
		for _, pt := range cfg.Points {
			if pt.ShmID > 0 {
				addrMap[pt.Addr] = pt.ShmID
			}
		}

		listener, err := net.Listen("tcp", fmt.Sprintf(":%d", cfg.Port))
		if err != nil {
			return newError(fmt.Sprintf("PORT_BIND_FAILED: instance '%s': cannot bind port %d: %v", cfg.ID, cfg.Port, err)), nil, nil
		}

		ist := &instanceState{
			cfg:      cfg,
			listener: listener,
			addrMap:  addrMap,
			quit:     make(chan struct{}),
		}
		newInstances = append(newInstances, ist)
		go runServer(ist, shmData)
	}

	state.instances = newInstances

	return newResult("success"), nil, nil
}

func statusHandler(ctx context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	if !state.started.Load() {
		return newError("SERVICE_NOT_READY: start has not been called"), nil
	}

	type instStatus struct {
		ID          string `json:"id"`
		Name        string `json:"name"`
		Port        int    `json:"port"`
		State       string `json:"state"`
		Connections int    `json:"connections"`
		PointsCount int    `json:"points_count"`
		Stats       struct {
			PacketsReceived uint64 `json:"packets_received"`
			ItemsReceived   uint64 `json:"items_received"`
			ItemsWritten    uint64 `json:"items_written"`
			ItemsDropped    uint64 `json:"items_dropped"`
			ParseErrors     uint64 `json:"parse_errors"`
		} `json:"stats"`
	}

	state.mu.Lock()
	defer state.mu.Unlock()

	var result []instStatus
	for _, ist := range state.instances {
		s := instStatus{
			ID:          ist.cfg.ID,
			Name:        ist.cfg.Name,
			Port:        ist.cfg.Port,
			PointsCount: len(ist.cfg.Points),
		}
		if ist.paused.Load() {
			s.State = "paused"
		} else {
			s.State = "running"
		}
		s.Stats.PacketsReceived = atomic.LoadUint64(&ist.stats.packetsReceived)
		s.Stats.ItemsReceived = atomic.LoadUint64(&ist.stats.itemsReceived)
		s.Stats.ItemsWritten = atomic.LoadUint64(&ist.stats.itemsWritten)
		s.Stats.ItemsDropped = atomic.LoadUint64(&ist.stats.itemsDropped)
		s.Stats.ParseErrors = atomic.LoadUint64(&ist.stats.parseErrors)
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
		&mcp.Implementation{Name: "c4_asfp2_server", Version: "0.1.0"},
		nil,
	)

	mcp.AddTool(server,
		&mcp.Tool{Name: "start", Description: "Start ASFP2 server instances"},
		startHandler,
	)

	mcp.AddTool(server,
		&mcp.Tool{
			Name:        "pause",
			Description: "Pause data reception for specified shm_ids (empty=all)",
			InputSchema: json.RawMessage(`{"type":"object","properties":{"shm_ids":{"type":"array","items":{"type":"integer","minimum":1}}}}`),
		},
		pauseHandler,
	)

	mcp.AddTool(server,
		&mcp.Tool{
			Name:        "resume",
			Description: "Resume data reception and reload configuration",
			InputSchema: json.RawMessage(`{"type":"object","properties":{"shm_ids":{"type":"array","items":{"type":"integer","minimum":1}},"new_max_points":{"type":"integer"}}}`),
		},
		resumeHandler,
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
