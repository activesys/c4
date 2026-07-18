package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"os"
	"strings"
	"sync"
	"sync/atomic"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"golang.org/x/sys/unix"

	"c4/mcp/internal/shm"
	"c4/mcp/internal/protocol"
)

// ──────────────────────────────────────────────
//  Configuration types
// ──────────────────────────────────────────────

type serverPoint struct {
	ID    string `json:"id"`
	Addr  uint32 `json:"addr"`
	ShmID int    `json:"shm_id"`
}

type serverInstance struct {
	Name        string        `json:"name"`
	ID          string        `json:"id"`
	Port        int           `json:"port"`
	T1          int           `json:"t1"`
	T2          int           `json:"t2"`
	ForwardKack uint8         `json:"forward_kack"`
	InverseKeep uint8         `json:"inverse_keep"`
	Points      []serverPoint `json:"points"`
}

type serverConfig struct {
	C4ASFP2Server []serverInstance `json:"c4_asfp2_server"`
}

// ──────────────────────────────────────────────
//  Protocol constants (shared via c4/mcp/internal/protocol)
// ──────────────────────────────────────────────

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
	cfg      serverInstance
	listener net.Listener
	addrMap  map[uint32]int // addr → shm_id
	stats    instanceStats
	quit     chan struct{}
	wg       sync.WaitGroup // tracks runServer goroutine
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

func loadConfig(req *mcp.CallToolRequest) ([]serverInstance, string, error) {
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
	var instances []serverInstance
	if err := json.Unmarshal(rawJSON, &instances); err != nil {
		return nil, "", fmt.Errorf("CONFIG_PARSE_ERROR: failed to parse 'c4_asfp2_server' section: %v", err)
	}

	return instances, configPath, nil
}

func validateConfig(instances []serverInstance) error {
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
			if pt.Addr > protocol.MaxAddr {
				return fmt.Errorf("CONFIG_PARSE_ERROR: addr %d exceeds max protocol.MaxAddr", pt.Addr)
			}
		}
	}
	return nil
}

// ──────────────────────────────────────────────
//  Shared memory
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
	magic := binary.NativeEndian.Uint32(hdrData[0:])
	if magic != shm.Magic {
		unix.Munmap(hdrData)
		unix.Close(fd)
		return nil, 0, fmt.Errorf("SHM_CORRUPTED: header magic is invalid (got 0x%08X, expected 0x%08X)", magic, shm.Magic)
	}
	maxPoints := binary.NativeEndian.Uint32(hdrData[shm.HdrOffMaxPoints:])
	unix.Munmap(hdrData)

	totalSize := int64(int(maxPoints)+1) * shm.BlockSize
	data, err := unix.Mmap(fd, 0, int(totalSize), unix.PROT_READ|unix.PROT_WRITE, unix.MAP_SHARED)
	if err != nil {
		unix.Close(fd)
		return nil, 0, fmt.Errorf("SHM_OPEN_FAILED: mmap failed: %v", err)
	}

	return data, fd, nil
}

// ──────────────────────────────────────────────
//  ASFP2 Parser
// ──────────────────────────────────────────────

var bufPool = sync.Pool{New: func() any { return make([]byte, 65536) }}

func parseASFP2Data(conn net.Conn, inst *instanceState, shmData []byte) {
	defer conn.Close()

	tmp := bufPool.Get().([]byte)
	defer bufPool.Put(tmp)

	var remain []byte

	for {
		n, err := conn.Read(tmp)
		if err != nil {
			return
		}
		if n < 1 {
			continue
		}

		remain = append(remain, tmp[:n]...)
		if len(remain) > 1<<20 {
			return
		}
		pos := 0
		for pos < len(remain) {
			firstByte := remain[pos]

			// Heartbeat: 'K' prefix
			if firstByte == 'K' {
				if pos+4 <= len(remain) && string(remain[pos:pos+4]) == "KEEP" {
					conn.Write([]byte{inst.cfg.ForwardKack})
					pos += 4
					continue
				}
				if pos+4 <= len(remain) && string(remain[pos:pos+4]) == "KACK" {
					pos += 4
					continue
				}
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
			if pos+8 > len(remain) {
				break
			}
			pktStart := pos
			flag := string(remain[pos : pos+8])
			var versionStr string
			switch flag {
			case protocol.FlagV200:
				versionStr = protocol.FlagV200
			case protocol.FlagV210:
				versionStr = protocol.FlagV210
			case protocol.FlagV211:
				versionStr = protocol.FlagV211
			default:
				pos++
				atomic.AddUint64(&inst.stats.parseErrors, 1)
				continue
			}
			pos += 8

			// Parse header: Length + Count + Attribute
			if pos+8 > len(remain) {
				pos = pktStart
				break
			}

			var length, count int
			var attribute uint32

			if versionStr == protocol.FlagV200 {
				length = int(binary.BigEndian.Uint16(remain[pos : pos+2]))
				count = int(binary.BigEndian.Uint16(remain[pos+2 : pos+4]))
				attribute = binary.BigEndian.Uint32(remain[pos+4 : pos+8])
			} else {
				lengthLow := int(binary.BigEndian.Uint16(remain[pos : pos+2]))
				lengthExt := int(binary.BigEndian.Uint16(remain[pos+4 : pos+6]))
				length = lengthLow | (lengthExt << 16)
				count = int(binary.BigEndian.Uint16(remain[pos+2 : pos+4]))
				attribute = binary.BigEndian.Uint32(remain[pos+4 : pos+8])
				attribute = (attribute & 0x0000FFFF) | (uint32(lengthExt) << 16)
			}
			pos += 8

			if length < 16 || pktStart+length > len(remain) {
				pos = pktStart
				break
			}

			// Parse Mutable
			var mutableKey uint32
			var mutableType uint8
			var mutableTimestamp uint64
			hasKey := (attribute & protocol.AttrKeySequence) != 0
			hasType := (attribute & protocol.AttrSameDataType) != 0
			hasTs := (attribute & protocol.AttrSameTimestamp) != 0

			if hasType {
				mutableType = remain[pos]
				pos++
			}
			if hasKey {
				mutableKey = uint32(remain[pos])<<16 | uint32(remain[pos+1])<<8 | uint32(remain[pos+2])
				pos += 3
			}
			if hasTs {
				mutableTimestamp = binary.BigEndian.Uint64(remain[pos : pos+8])
				pos += 8
			}

			// Drop entire packet if same data type is variable-length
			if hasType && protocol.VariableTypes[mutableType] {
				atomic.AddUint64(&inst.stats.parseErrors, 1)
				pos = pktStart + length
				continue
			}

			// BIT compression mode
			if hasKey && hasType && hasTs && (mutableType == protocol.TypeBoolean || mutableType == protocol.TypeBit) {
				compressedBytes := (count + 7) / 8
				if pos+compressedBytes > len(remain) {
					pos = pktStart
					break
				}
				for i := 0; i < count; i++ {
					byteIdx := i / 8
					bitIdx := i % 8
					bit := (remain[pos+byteIdx] >> bitIdx) & 1
					addr := mutableKey + uint32(i)
					shmID, ok := inst.addrMap[addr]
					if ok {
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
					if pos >= len(remain) {
						pos = pktStart
						break
					}
					itemType = remain[pos]
					pos++
				}
				if !hasKey {
					if pos+3 > len(remain) {
						pos = pktStart
						break
					}
					itemKey = uint32(remain[pos])<<16 | uint32(remain[pos+1])<<8 | uint32(remain[pos+2])
					pos += 3
				}
				if !hasTs {
					if pos+8 > len(remain) {
						pos = pktStart
						break
					}
					itemTs = binary.BigEndian.Uint64(remain[pos : pos+8])
					pos += 8
				}

				atomic.AddUint64(&inst.stats.itemsReceived, 1)

				// Skip variable-length items
				if protocol.VariableTypes[itemType] {
					atomic.AddUint64(&inst.stats.itemsDropped, 1)
					continue
				}

				valueSize := protocol.TypeByteSize(itemType)
				if pos+valueSize > len(remain) {
					pos = pktStart
					break
				}

				value, valueSize := decodePacketValue(remain, pos, itemType, versionStr)
				pos += valueSize

				shmID, ok := inst.addrMap[itemKey]
				if ok {
					writeBlock(shmData, shmID, itemType, itemTs, value, valueSize)
					atomic.AddUint64(&inst.stats.itemsWritten, 1)
				} else {
					atomic.AddUint64(&inst.stats.itemsDropped, 1)
				}
			}

			// Check if data loop exited early (partial item)
			if pos == pktStart {
				break
			}
			atomic.AddUint64(&inst.stats.packetsReceived, 1)
		}
		remain = remain[pos:]
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
		for (m & 0x400) == 0 {
			m <<= 1
			e++
		}
		m &= 0x3FF
		// e holds shift count. biased float32 exponent = 113 - e
		// (subnormal effective = -14 - e, bias 127 → -14 - e + 127 = 113 - e)
		e = 113 - e
		return (s << 31) | (e << 23) | (m << 13)
	} else if e == 0x1F {
		return (s << 31) | (0xFF << 23) | (m << 13)
	}

	e = e - 15 + 127
	return (s << 31) | (e << 23) | (m << 13)
}

func decodePacketValue(buf []byte, pos int, itemType uint8, versionStr string) (uint64, int) {
	valueSize := protocol.TypeByteSize(itemType)
	isV211 := versionStr == protocol.FlagV211

	var value uint64
	switch itemType {
	case protocol.TypeBoolean, protocol.TypeBit:
		value = uint64(buf[pos] & 1)
	case protocol.TypeInt8:
		v := int8(buf[pos])
		value = uint64(int64(v))
	case protocol.TypeUint8:
		value = uint64(buf[pos])
	case protocol.TypeInt16:
		v := int16(binary.BigEndian.Uint16(buf[pos : pos+2]))
		value = uint64(int64(v))
	case protocol.TypeUint16:
		value = uint64(binary.BigEndian.Uint16(buf[pos : pos+2]))
	case protocol.TypeInt32:
		v := int32(binary.BigEndian.Uint32(buf[pos : pos+4]))
		value = uint64(int64(v))
	case protocol.TypeUint32:
		value = uint64(binary.BigEndian.Uint32(buf[pos : pos+4]))
	case protocol.TypeInt64, protocol.TypeUint64:
		value = binary.BigEndian.Uint64(buf[pos : pos+8])
	case protocol.TypeFloat16:
		if isV211 {
			bits := binary.BigEndian.Uint16(buf[pos : pos+2])
			value = uint64(float16ToFloat32(bits))
		} else {
			bits := binary.NativeEndian.Uint16(buf[pos : pos+2])
			value = uint64(float16ToFloat32(bits))
		}
	case protocol.TypeFloat32:
		if isV211 {
			value = uint64(binary.BigEndian.Uint32(buf[pos : pos+4]))
		} else {
			value = uint64(binary.NativeEndian.Uint32(buf[pos : pos+4]))
		}
	case protocol.TypeFloat64:
		if isV211 {
			value = binary.BigEndian.Uint64(buf[pos : pos+8])
		} else {
			value = binary.NativeEndian.Uint64(buf[pos : pos+8])
		}
	}
	return value, valueSize
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
	magic := binary.NativeEndian.Uint32(shmData[off+shm.BlkOffMagic:])
	if magic != shm.Magic {
		return
	}

	// Activate block on first write
	if shmData[off+shm.BlkOffState] == 0 {
		shmData[off+shm.BlkOffState] = 1
		binary.NativeEndian.PutUint64(shmData[off+shm.BlkOffWriteSeq:], 0)
	}

	// Seqlock: increment to odd
	writeSeq := binary.NativeEndian.Uint64(shmData[off+shm.BlkOffWriteSeq:])
	binary.NativeEndian.PutUint64(shmData[off+shm.BlkOffWriteSeq:], writeSeq+1)

	// Write data
	binary.NativeEndian.PutUint64(shmData[off+shm.BlkOffTimestamp:], timestamp)
	shmData[off+shm.BlkOffType] = dataType
	binary.NativeEndian.PutUint64(shmData[off+shm.BlkOffValue:], value)

	// Seqlock: increment to even
	binary.NativeEndian.PutUint64(shmData[off+shm.BlkOffWriteSeq:], writeSeq+2)
}

// ──────────────────────────────────────────────
//  MCP Tool Handlers
// ──────────────────────────────────────────────

func startHandler(ctx context.Context, req *mcp.CallToolRequest, input struct{}) (*mcp.CallToolResult, any, error) {
	if state.started.Load() {
		return newError("ALREADY_RUNNING: start has already been called and service is running, call stop first"), nil, nil
	}

	instances, _, err := loadConfig(req)
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

	shmData, shmFd, err := attachShm()
	if err != nil {
		return newError(err.Error()), nil, nil
	}

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
		unix.Munmap(shmData)
		unix.Close(shmFd)
		return newError(lastErr), nil, nil
	}

	// Start goroutines
	for _, ist := range instancesState {
		ist.wg.Add(1)
		go func() {
			defer ist.wg.Done()
			runServer(ist, shmData)
		}()
	}

	state.mu.Lock()
	state.instances = instancesState
	state.shmData = shmData
	state.shmFd = shmFd
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

func stopHandler(ctx context.Context, req *mcp.CallToolRequest, input struct{}) (*mcp.CallToolResult, any, error) {
	if !state.started.Load() {
		return newError("SERVICE_NOT_READY: start has not been called"), nil, nil
	}

	state.mu.Lock()
	defer state.mu.Unlock()

	for _, ist := range state.instances {
		close(ist.quit)
		ist.listener.Close()
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
		s.State = "running"
		s.Stats.PacketsReceived = atomic.LoadUint64(&ist.stats.packetsReceived)
		s.Stats.ItemsReceived = atomic.LoadUint64(&ist.stats.itemsReceived)
		s.Stats.ItemsWritten = atomic.LoadUint64(&ist.stats.itemsWritten)
		s.Stats.ItemsDropped = atomic.LoadUint64(&ist.stats.itemsDropped)
		s.Stats.ParseErrors = atomic.LoadUint64(&ist.stats.parseErrors)
		result = append(result, s)
	}

	jsonData, err := json.Marshal(map[string]any{"instances": result})
	if err != nil {
		return newError("INTERNAL_ERROR: failed to marshal status: " + err.Error()), nil
	}
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
			Name:        "stop",
			Description: "Stop all ASFP2 server instances and release resources",
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
