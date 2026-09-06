package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	stdlog "log"
	"log/slog"
	"net"
	"os"
	"regexp"
	"sync"
	"sync/atomic"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"golang.org/x/sys/unix"

	"c4/mcp/internal/logger"
	"c4/mcp/internal/protocol"
	"c4/mcp/internal/shm"
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
	quitOnce sync.Once // guards close(quit) — statsLoop 与 stop 共用同一路径
	wg       sync.WaitGroup
	log      *slog.Logger // inst-scoped logger
	connsMu  sync.Mutex
	conns    map[net.Conn]struct{} // live connections（stop 时布读超时释放）
	// first-occurrence flags for rate-limited warnings/errors (§9.6)
	parseErrLogged    atomic.Bool
	keyMissLogged     atomic.Bool
	shmWriteErrLogged atomic.Bool
}

// stop closes quit exactly once; safe to call from statsLoop and stopHandler.
func (ist *instanceState) stop() {
	ist.quitOnce.Do(func() { close(ist.quit) })
}

func (ist *instanceState) addConn(c net.Conn) {
	ist.connsMu.Lock()
	ist.conns[c] = struct{}{}
	ist.connsMu.Unlock()
}

func (ist *instanceState) removeConn(c net.Conn) {
	ist.connsMu.Lock()
	delete(ist.conns, c)
	ist.connsMu.Unlock()
}

// closeConns unblocks connection goroutines blocked in conn.Read by arming a
// read deadline on every live connection.
func (ist *instanceState) closeConns() {
	ist.connsMu.Lock()
	defer ist.connsMu.Unlock()
	for c := range ist.conns {
		c.SetReadDeadline(time.Now())
	}
}

type serverState struct {
	started   atomic.Bool
	instances []*instanceState
	mu        sync.Mutex
	shmData   []byte
	shmFd     int
}

var state = &serverState{}

// log is the service-scoped structured logger (journald, five levels, §9).
var log *slog.Logger

// ──────────────────────────────────────────────
//  Config loading
// ──────────────────────────────────────────────

func loadConfig(configPath string) ([]serverInstance, error) {
	data, err := os.ReadFile(configPath)
	if err != nil {
		return nil, fmt.Errorf("CONFIG_PATH_MISSING: cannot read config file: %v", err)
	}

	var fullCfg map[string]any
	if err := json.Unmarshal(data, &fullCfg); err != nil {
		return nil, fmt.Errorf("CONFIG_PARSE_ERROR: failed to parse config JSON: %v", err)
	}

	section, ok := fullCfg["c4_asfp2_server"]
	if !ok {
		return nil, fmt.Errorf("CONFIG_PARSE_ERROR: 'c4_asfp2_server' section not found in config")
	}

	rawJSON, _ := json.Marshal(section)
	var instances []serverInstance
	if err := json.Unmarshal(rawJSON, &instances); err != nil {
		return nil, fmt.Errorf("CONFIG_PARSE_ERROR: failed to parse 'c4_asfp2_server' section: %v", err)
	}

	// KeepAlive 字节缺省归一化：forward_kack 取规范典型值 0xFF。显式或隐式为 0 均视为
	// 未配置——0 与 inverse_keep 的典型值冲突，客户端将无法区分 kack 与反向 KEEP，
	// 导致其 T2 应答等待永远无法清除（ASFP2 2.1.1 规范：典型 255 / 0）。
	for i := range instances {
		if instances[i].ForwardKack == 0 {
			instances[i].ForwardKack = 255
		}
	}

	return instances, nil
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
		// ASFP2 spec: T2 must be strictly less than T1 (t2=0 = no-ack-wait mode)
		if inst.T1 > 0 && inst.T2 > 0 && inst.T2 >= inst.T1 {
			return fmt.Errorf("CONFIG_PARSE_ERROR: instance '%s' violates T2 < T1 (t1=%d, t2=%d)", inst.ID, inst.T1, inst.T2)
		}
		// 反向 KEEP 字节不得与正向 kack 字节相同，否则客户端无法区分两种 1 字节帧
		if inst.InverseKeep == inst.ForwardKack {
			return fmt.Errorf("CONFIG_PARSE_ERROR: instance '%s': inverse_keep (%d) must differ from forward_kack (%d)",
				inst.ID, inst.InverseKeep, inst.ForwardKack)
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

func attachShm(instanceID string) ([]byte, int, error) {
	shmPath := shm.ShmPath(instanceID)

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
//  Keepalive timers (ASFP2 2.1.1, libasfp2 semantics)
// ──────────────────────────────────────────────

// kaClock implements the ASFP2 keepalive timers: T1 fires when the connection
// has been idle for t1 seconds (send keepalive + arm T2); T2 fires when no
// acknowledgement arrived within t2 seconds (link presumed dead).
// t1=0 disables keepalive entirely; t1>0 with t2=0 sends keeps without
// awaiting an ack (no liveness detection). Spec constraint: t2 < t1.
type kaClock struct {
	mu   sync.Mutex
	t1   time.Duration
	t2   time.Duration
	t1At time.Time
	t2At time.Time
	t2On bool
}

func newKAClock(t1, t2 int) *kaClock {
	return &kaClock{t1: time.Duration(t1) * time.Second, t2: time.Duration(t2) * time.Second}
}

// resetT1 restarts the T1 idle timer (no-op when keepalive is disabled).
func (k *kaClock) resetT1() {
	if k.t1 <= 0 {
		return
	}
	k.mu.Lock()
	k.t1At = time.Now().Add(k.t1)
	k.mu.Unlock()
}

// armT2 starts waiting for the keepalive acknowledgement (no-op when t2=0).
func (k *kaClock) armT2() {
	if k.t2 <= 0 {
		return
	}
	k.mu.Lock()
	k.t2At = time.Now().Add(k.t2)
	k.t2On = true
	k.mu.Unlock()
}

// stopT2 cancels a pending acknowledgement wait.
func (k *kaClock) stopT2() {
	k.mu.Lock()
	k.t2On = false
	k.mu.Unlock()
}

// poll returns "t2" when the ack wait expired (link dead), "t1" when the
// idle timer expired (send keepalive), or "".
func (k *kaClock) poll() string {
	now := time.Now()
	k.mu.Lock()
	defer k.mu.Unlock()
	if k.t2On && !now.Before(k.t2At) {
		return "t2"
	}
	if k.t1 > 0 && !now.Before(k.t1At) {
		return "t1"
	}
	return ""
}

// ──────────────────────────────────────────────
//  ASFP2 Parser
// ──────────────────────────────────────────────

var bufPool = sync.Pool{New: func() any { return make([]byte, 65536) }}

func parseASFP2Data(conn net.Conn, inst *instanceState, shmData []byte) {
	defer func() {
		if r := recover(); r != nil {
			inst.log.Error("panic_recovered", "goroutine", "connection", "err", fmt.Sprint(r))
		}
	}()
	defer conn.Close()

	tmp := bufPool.Get().([]byte)
	defer bufPool.Put(tmp)

	var remain []byte

	// Per-connection keepalive timers — T1 starts on connection establishment.
	ka := newKAClock(inst.cfg.T1, inst.cfg.T2)
	ka.resetT1()
	remote := ""
	if conn.RemoteAddr() != nil {
		remote = conn.RemoteAddr().String()
	}

	for {
		// Keepalive timer poll: fires at 1s granularity via the read deadline wakeups
		switch ka.poll() {
		case "t2":
			// T2 expired: no KACK from client — link presumed dead
			inst.log.Warn("keepalive_timeout", "remote", remote)
			return
		case "t1":
			// T1 expired: idle — send reverse keepalive (1 byte, inverse_keep)
			conn.SetWriteDeadline(time.Now().Add(time.Second))
			if _, err := conn.Write([]byte{inst.cfg.InverseKeep}); err != nil {
				return
			}
			conn.SetWriteDeadline(time.Time{})
			inst.log.Debug("keepalive_exchanged", "dir", "send_keep")
			ka.resetT1()
			ka.armT2()
		}

		// 周期读超时：空闲连接每秒醒来检查 quit，保证 stop 时 goroutine 可退出
		conn.SetReadDeadline(time.Now().Add(time.Second))
		n, err := conn.Read(tmp)
		if ne, ok := err.(net.Error); ok && ne.Timeout() {
			select {
			case <-inst.quit:
				return
			default:
			}
			continue
		}
		if err != nil {
			return
		}
		if n < 1 {
			continue
		}

		remain = append(remain, tmp[:n]...)
		// 2MiB：高于 v211 合法最大包（16+12+65535×20 ≈ 1.25MiB），仅对真正失步的流丢弃
		if len(remain) > 1<<21 {
			atomic.AddUint64(&inst.stats.parseErrors, 1)
			if !inst.parseErrLogged.Swap(true) {
				inst.log.Warn("parse_error", "reason", "desync_buffer_overflow", "raw_len", len(remain))
			}
			return
		}
		pos := 0
		for pos < len(remain) {
			firstByte := remain[pos]

			// Heartbeat: 'K' prefix
			if firstByte == 'K' {
				if pos+4 <= len(remain) && string(remain[pos:pos+4]) == "KEEP" {
					// 写超时：对端零窗口/僵死时 Write 可长时间阻塞，避免拖住 stop 的 wg.Wait
					conn.SetWriteDeadline(time.Now().Add(time.Second))
					conn.Write([]byte{inst.cfg.ForwardKack})
					conn.SetWriteDeadline(time.Time{})
					inst.log.Debug("keepalive_exchanged", "dir", "recv_keep", "reply", "kack")
					// 收到对端心跳证明链路存活：重启 T1（libasfp2 after_parse 语义）
					ka.resetT1()
					pos += 4
					continue
				}
				if pos+4 <= len(remain) && string(remain[pos:pos+4]) == "KACK" {
					inst.log.Debug("keepalive_exchanged", "dir", "recv_kack")
					// 反向心跳应答：停 T2，重启 T1
					ka.stopT2()
					ka.resetT1()
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
				if !inst.parseErrLogged.Swap(true) {
					inst.log.Warn("parse_error", "reason", "bad_marker", "byte", firstByte, "raw_len", len(remain))
				}
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
				if !inst.parseErrLogged.Swap(true) {
					inst.log.Warn("parse_error", "reason", "bad_flag", "flag", flag, "raw_len", len(remain))
				}
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
				if !inst.parseErrLogged.Swap(true) {
					inst.log.Warn("parse_error", "reason", "varlen_type_packet", "type", mutableType, "raw_len", length)
				}
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
						if ok2, reason := writeBlock(shmData, shmID, mutableType, mutableTimestamp, uint64(bit), 1); ok2 {
							atomic.AddUint64(&inst.stats.itemsWritten, 1)
						} else if !inst.shmWriteErrLogged.Swap(true) {
							inst.log.Error("shm_write_failed", "shm_id", shmID, "reason", reason)
						}
					} else {
						atomic.AddUint64(&inst.stats.itemsDropped, 1)
						if !inst.keyMissLogged.Swap(true) {
							inst.log.Warn("key_not_mapped", "addr", addr)
						}
					}
					atomic.AddUint64(&inst.stats.itemsReceived, 1)
				}
				pos += compressedBytes
				atomic.AddUint64(&inst.stats.packetsReceived, 1)
				inst.log.Debug("packet_header", "type", versionStr, "count", count,
					"key_range", fmt.Sprintf("%d-%d", mutableKey, mutableKey+uint32(count-1)))
				continue
			}

			// Parse Data items
			firstKey := uint32(0)
			lastKey := uint32(0)
			for i := 0; i < count; i++ {
				itemType := mutableType
				itemKey := mutableKey + uint32(i)
				if i == 0 {
					firstKey = itemKey
				}
				lastKey = itemKey
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
					if ok2, reason := writeBlock(shmData, shmID, itemType, itemTs, value, valueSize); ok2 {
						atomic.AddUint64(&inst.stats.itemsWritten, 1)
					} else if !inst.shmWriteErrLogged.Swap(true) {
						inst.log.Error("shm_write_failed", "shm_id", shmID, "reason", reason)
					}
				} else {
					atomic.AddUint64(&inst.stats.itemsDropped, 1)
					if !inst.keyMissLogged.Swap(true) {
						inst.log.Warn("key_not_mapped", "addr", itemKey)
					}
				}
			}

			// Check if data loop exited early (partial item)
			if pos == pktStart {
				break
			}
			atomic.AddUint64(&inst.stats.packetsReceived, 1)
			inst.log.Debug("packet_header", "type", versionStr, "count", count,
				"key_range", fmt.Sprintf("%d-%d", firstKey, lastKey))
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

func writeBlock(shmData []byte, shmID int, dataType uint8, timestamp uint64, value uint64, valueSize int) (bool, string) {
	off := shmID * shm.BlockSize
	if off+shm.BlockSize > len(shmData) {
		return false, "out_of_range"
	}

	// Verify magic
	magic := binary.NativeEndian.Uint32(shmData[off+shm.BlkOffMagic:])
	if magic != shm.Magic {
		return false, "magic_mismatch"
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
	writeValue(shmData, off+shm.BlkOffValue, value, valueSize)

	// Seqlock: increment to even
	binary.NativeEndian.PutUint64(shmData[off+shm.BlkOffWriteSeq:], writeSeq+2)
	return true, ""
}

// writeValue writes a value in native byte order into the low `valueSize` bytes
// of the 8-byte value field, zeroing the high bytes (c4_architecture.md §2.2.3).
func writeValue(shmData []byte, off int, value uint64, valueSize int) {
	for i := 0; i < 8; i++ {
		shmData[off+i] = 0
	}
	var buf [8]byte
	binary.NativeEndian.PutUint64(buf[:], value)
	copy(shmData[off:off+valueSize], buf[:valueSize])
}

// ──────────────────────────────────────────────
//  MCP Tool Handlers
// ──────────────────────────────────────────────

type ServerStartInput struct {
	InstanceID string `json:"instance_id" jsonschema:"required,instance identifier matching ^c4_[a-zA-Z0-9]+$ (POSIX shm name)"`
	ConfigPath string `json:"config_path" jsonschema:"required,absolute path to config.json"`
}

// validateInstanceID reports whether id is a valid C4 instance identifier,
// i.e. the POSIX shared memory name it must directly attach to.
func validateInstanceID(id string) bool {
	return regexp.MustCompile(`^c4_[a-zA-Z0-9]+$`).MatchString(id)
}

func startHandler(ctx context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	if state.started.Load() {
		return newError("ALREADY_RUNNING: start has already been called and service is running, call stop first"), nil
	}

	var args map[string]any
	if err := json.Unmarshal(req.Params.Arguments, &args); err != nil {
		return newError("CONFIG_PATH_MISSING: cannot parse arguments"), nil
	}
	configPath, _ := args["config_path"].(string)
	if configPath == "" {
		return newError("CONFIG_PATH_MISSING: config_path is required"), nil
	}
	instanceID, _ := args["instance_id"].(string)
	if !validateInstanceID(instanceID) {
		return newError("INVALID_INSTANCE_ID: instance_id must match ^c4_[a-zA-Z0-9]+$"), nil
	}
	instances, err := loadConfig(configPath)
	if err != nil {
		log.Warn("config_invalid", "err", err.Error())
		return newError(err.Error()), nil
	}

	if err := validateConfig(instances); err != nil {
		log.Warn("config_invalid", "err", err.Error())
		return newError(err.Error()), nil
	}

	pointsTotal := 0
	for _, cfg := range instances {
		pointsTotal += len(cfg.Points)
	}
	log.Info("config_loaded", "instances", len(instances), "points_total", pointsTotal)

	// Empty instances array is valid — start succeeds with no port listeners
	if len(instances) == 0 {
		state.started.Store(true)
		return newResult("success"), nil
	}

	shmData, shmFd, err := attachShm(instanceID)
	if err != nil {
		log.Error("shm_attach_failed", "instance_id", instanceID, "err", err.Error())
		return newError(err.Error()), nil
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
			log.Error("listen_failed", "inst", cfg.ID, "port", cfg.Port, "err", err.Error())
			lastErr = fmt.Sprintf("PORT_BIND_FAILED: instance '%s': cannot bind port %d: %v", cfg.ID, cfg.Port, err)
			break
		}

		ist := &instanceState{
			cfg:      cfg,
			listener: listener,
			addrMap:  addrMap,
			quit:     make(chan struct{}),
			log:      log.With("inst", cfg.ID),
			conns:    make(map[net.Conn]struct{}),
		}
		instancesState = append(instancesState, ist)
	}

	if lastErr != "" {
		for _, ist := range instancesState {
			ist.listener.Close()
		}
		unix.Munmap(shmData)
		unix.Close(shmFd)
		return newError(lastErr), nil
	}

	// Start goroutines — instance_started 仅在全部实例绑定成功后发出（与 instance_stopped 配对）
	for _, ist := range instancesState {
		ist.log.Info("instance_started", "listen", fmt.Sprintf(":%d", ist.cfg.Port), "points", len(ist.cfg.Points))
		ist.wg.Add(1)
		go func() {
			defer func() {
				if r := recover(); r != nil {
					ist.log.Error("panic_recovered", "goroutine", "instance", "err", fmt.Sprint(r))
				}
			}()
			defer ist.wg.Done()
			runServer(ist, shmData)
		}()
		ist.wg.Add(1)
		go func() {
			defer ist.wg.Done()
			statsLoop(ist, shmData)
		}()
	}

	state.mu.Lock()
	state.instances = instancesState
	state.shmData = shmData
	state.shmFd = shmFd
	state.started.Store(true)
	state.mu.Unlock()

	return newResult("success"), nil
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
		ist.wg.Add(1)
		ist.addConn(conn)
		select {
		case <-ist.quit:
			ist.removeConn(conn)
			ist.wg.Done()
			conn.Close()
			return
		default:
		}
		remote := ""
		if conn.RemoteAddr() != nil {
			remote = conn.RemoteAddr().String()
		}
		ist.log.Info("client_connected", "remote", remote)
		connectedAt := time.Now()
		go func() {
			defer func() {
				ist.removeConn(conn)
				ist.wg.Done()
				ist.log.Info("client_disconnected", "remote", remote, "duration_s", time.Since(connectedAt).Seconds())
			}()
			parseASFP2Data(conn, ist, shmData)
		}()
	}
}

// statsLoop emits periodic instanceStats snapshots and watches for shared
// memory loss via the header magic (§9.5 stats_periodic / shm_lost). The
// watchdog runs on its own fixed cadence and is NOT disabled by
// C4_MCP_LOG_STATS_INTERVAL=0 — that knob only silences the stats line.
func statsLoop(ist *instanceState, shmData []byte) {
	statsIv := logger.StatsInterval()
	statsEnabled := statsIv > 0
	if statsIv <= 0 {
		statsIv = 60 * time.Second
	}
	ticker := time.NewTicker(statsIv)
	defer ticker.Stop()
	for {
		select {
		case <-ist.quit:
			return
		case <-ticker.C:
		}
		if binary.NativeEndian.Uint32(shmData[0:]) != shm.Magic {
			ist.log.Log(context.Background(), logger.LevelCrit, "shm_lost",
				"err", "shared memory header magic is invalid, data path interrupted")
			ist.stop()
			ist.listener.Close()
			ist.closeConns()
			return
		}
		if !statsEnabled {
			continue
		}
		ist.log.Info("stats_periodic",
			"packets", atomic.LoadUint64(&ist.stats.packetsReceived),
			"items_received", atomic.LoadUint64(&ist.stats.itemsReceived),
			"items_written", atomic.LoadUint64(&ist.stats.itemsWritten),
			"items_dropped", atomic.LoadUint64(&ist.stats.itemsDropped),
			"parse_errors", atomic.LoadUint64(&ist.stats.parseErrors),
		)
	}
}

func stopHandler(ctx context.Context, req *mcp.CallToolRequest, input struct{}) (*mcp.CallToolResult, any, error) {
	if !state.started.Load() {
		return newResult("success"), nil, nil
	}

	state.mu.Lock()
	defer state.mu.Unlock()

	for _, ist := range state.instances {
		ist.stop()
		ist.listener.Close()
		ist.closeConns()
		ist.wg.Wait()
		ist.log.Info("instance_stopped")
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
	log = logger.Init("c4_asfp2_server")

	server := mcp.NewServer(
		&mcp.Implementation{Name: "c4_asfp2_server", Version: "0.1.0"},
		nil,
	)

	server.AddTool(
		&mcp.Tool{
			Name:        "start",
			Description: "Start ASFP2 server instances",
			InputSchema: json.RawMessage(`{"type":"object","properties":{"instance_id":{"type":"string"},"config_path":{"type":"string"}},"required":["instance_id","config_path"]}`),
		},
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

	if err := server.Run(context.Background(), &mcp.StdioTransport{}); err != nil {
		stdlog.Fatal(err)
	}
}
