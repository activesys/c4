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
	"runtime"
	"sort"
	"strconv"
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

type clientPoint struct {
	Key   string `json:"key"`
	Addr  uint32 `json:"addr"`
	ShmID int    `json:"shm_id"`
}

type clientInstance struct {
	Name        string        `json:"name"`
	ID          string        `json:"id"`
	IP          string        `json:"ip"`
	Port        int           `json:"port"`
	T0          int           `json:"t0"`
	T1          int           `json:"t1"`
	T2          int           `json:"t2"`
	Smart       int           `json:"smart"`
	ForwardKack uint8         `json:"forward_kack"`
	InverseKeep uint8         `json:"inverse_keep"`
	Timer       int           `json:"timer"`
	Points      []clientPoint `json:"points"`
}

// ──────────────────────────────────────────────
//  Protocol constants (shared via c4/mcp/internal/protocol)
// ──────────────────────────────────────────────

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
	pointsSent   uint64
	itemsSkipped uint64
	smartSkipped uint64
	sendErrors   uint64
	reconnects   uint64
}

type instanceState struct {
	cfg         clientInstance
	conn        net.Conn
	quit        chan struct{}
	quitOnce    sync.Once      // guards close(quit) — statsLoop 与 stop 共用同一路径
	shmIDs      map[int]uint32 // shmID → addr
	ptKeys      map[int]string // shmID → 点位 key（日志用）
	sortedPts   []shmIDAddr    // addr-sorted iteration order
	lastSeen    map[int]uint64 // shmID → last write_seq
	stats       instanceStats
	mu          sync.Mutex     // guards conn
	writeMu     sync.Mutex     // serializes data/keepalive writes (write deadline cross-talk)
	ka          *kaClock       // keepalive timers (T1 idle / T2 ack wait)
	connDead    atomic.Bool    // receiver → sendRound: link presumed dead, reconnect now
	t0          time.Duration  // 连接定时器（断链/首连失败到重拨的等待时长，§T0）
	wg          sync.WaitGroup // tracks runSender/runReceiver/statsLoop goroutines
	log         *slog.Logger   // inst-scoped logger
	connectedAt time.Time      // 当前连接建立时刻
	downAt      time.Time      // 最近一次断连时刻
	connectWarn time.Time      // connect_failed 限频：上次告警时刻
	deadReason  string         // markConnDead 记录的断链原因（mu 保护）
	// first-occurrence flag for rate-limited warnings (§9.6); reconnect attempts
	encodeSkippedLogged atomic.Bool
	connectAttempt      atomic.Uint64
}

// stop closes quit exactly once; safe to call from statsLoop and stopHandler.
func (ist *instanceState) stop() {
	ist.quitOnce.Do(func() { close(ist.quit) })
}

type shmIDAddr struct {
	shmID int
	addr  uint32
}

type clientState struct {
	started   atomic.Bool
	instances []*instanceState
	mu        sync.Mutex
	shmData   []byte
	shmFd     int
}

var state = &clientState{}

// log is the service-scoped structured logger (journald, five levels, §9).
var log *slog.Logger

// ──────────────────────────────────────────────
//  Config loading
// ──────────────────────────────────────────────

func loadConfig(configPath string) ([]clientInstance, error) {
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
	var instances []clientInstance
	if err := json.Unmarshal(rawJSON, &instances); err != nil {
		return nil, fmt.Errorf("CONFIG_PARSE_ERROR: failed to parse 'c4_asfp2_client' section: %v", err)
	}

	// KeepAlive 字节缺省归一化：forward_kack 取规范典型值 0xFF。显式或隐式为 0 均视为
	// 未配置——客户端以该字节识别服务端的 kack 应答，0 与 inverse_keep 典型值冲突时
	// T2 应答等待将永远无法清除（ASFP2 2.1.1 规范：典型 255 / 0）。
	for i := range instances {
		if instances[i].ForwardKack == 0 {
			instances[i].ForwardKack = 255
		}
		// T0 连接定时器缺省 30s（对齐 mcp-registry 声明的 default）：断链/首连失败后
		// 以 T0 为周期后台重拨，连接建立后关闭（asfp2_specification.md §T0，图 814）。
		if instances[i].T0 <= 0 {
			instances[i].T0 = 30
		}
	}

	return instances, nil
}

func validateConfig(instances []clientInstance) error {
	for _, inst := range instances {
		if inst.IP == "" {
			return fmt.Errorf("CONFIG_PARSE_ERROR: instance '%s' has empty ip field", inst.Name)
		}
		if inst.Port <= 0 || inst.Port > 65535 {
			return fmt.Errorf("CONFIG_PARSE_ERROR: instance '%s' has invalid port %d", inst.Name, inst.Port)
		}
		for _, pt := range inst.Points {
			if pt.Addr > protocol.MaxAddr {
				return fmt.Errorf("CONFIG_PARSE_ERROR: addr %d exceeds max protocol.MaxAddr", pt.Addr)
			}
			if pt.ShmID == 0 {
				return fmt.Errorf("SHM_ID_NOT_ASSIGNED: point '%s' has shm_id=0, must be assigned by c4_shm_manager first", pt.Key)
			}
		}
		// ASFP2 spec: T2 must be strictly less than T1 (t2=0 = no-ack-wait mode)
		if inst.T1 > 0 && inst.T2 > 0 && inst.T2 >= inst.T1 {
			return fmt.Errorf("CONFIG_PARSE_ERROR: instance '%s' violates T2 < T1 (t1=%d, t2=%d)", inst.Name, inst.T1, inst.T2)
		}
		// ASFP2 spec: the inverse keepalive byte must differ from the forward
		// kack byte, otherwise the client cannot tell the two frames apart
		if inst.T1 > 0 && inst.InverseKeep == inst.ForwardKack {
			return fmt.Errorf("CONFIG_PARSE_ERROR: instance '%s': inverse_keep (%d) must differ from forward_kack (%d)",
				inst.Name, inst.InverseKeep, inst.ForwardKack)
		}
	}
	return nil
}

// instanceIDRegex validates the mandatory instance_id format (^c4_[a-zA-Z0-9]+$).
// instance_id doubles as the POSIX shared memory object name under /dev/shm.
var instanceIDRegex = regexp.MustCompile("^c4_[a-zA-Z0-9]+$")

func validateInstanceID(id string) bool {
	return instanceIDRegex.MatchString(id)
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
	magic := binary.NativeEndian.Uint32(shmData[off+shm.BlkOffMagic:])
	if magic != shm.Magic {
		return 0, 0, 0, 0, false
	}

	// Check state: block not activated
	if shmData[off+shm.BlkOffState] == 0 {
		return 0, 0, 0, 0, false
	}

	for i := 0; i < 100; i++ {
		s1 := binary.NativeEndian.Uint64(shmData[off+shm.BlkOffWriteSeq:])
		if s1&1 != 0 {
			// Writer in progress, skip
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
//  Encode ASFPV211 packet
// ──────────────────────────────────────────────

// shmItem represents a data item read from shared memory, ready for encoding.
type shmItem struct {
	shmID     int
	addr      uint32
	dataType  uint8
	timestamp uint64
	value     uint64
	seq       uint64
}

// encodeASFPV211 builds a single ASFPV211 data packet for a subgroup of items.
// The subgroup is guaranteed to have consecutive addrs (KEY_SEQUENCE=1).
// Returns the encoded byte slice.
func detectAttributes(items []shmItem, smart int) (attr uint32, hasKey, hasType, hasTs bool) {
	for i := range items {
		if smart == 1 {
			items[i].timestamp = (items[i].timestamp / 1000) * 1000
		}
	}

	attr = protocol.AttrKeySequence

	sameDataType := true
	for i := 1; i < len(items); i++ {
		if items[i].dataType != items[0].dataType {
			sameDataType = false
			break
		}
	}
	if sameDataType {
		attr |= protocol.AttrSameDataType
	}

	sameTimestamp := true
	for i := 1; i < len(items); i++ {
		if items[i].timestamp != items[0].timestamp {
			sameTimestamp = false
			break
		}
	}
	if sameTimestamp {
		attr |= protocol.AttrSameTimestamp
	}

	return attr, true, (attr & protocol.AttrSameDataType) != 0, (attr & protocol.AttrSameTimestamp) != 0
}

func encodeASFPV211(items []shmItem, smart int) []byte {
	count := len(items)
	if count == 0 {
		return nil
	}

	attr, hasKey, hasType, hasTs := detectAttributes(items, smart)

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
	bitCompression := hasKey && hasType && hasTs && (dataType == protocol.TypeBoolean || dataType == protocol.TypeBit)

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
			dataSize += protocol.TypeByteSize(items[i].dataType)
		}
	}

	totalLength := 16 + mutableSize + dataSize
	lengthLow := totalLength & 0xFFFF
	attrHigh := (totalLength >> 16) & 0xFFFF

	buf := make([]byte, totalLength)

	// ── Header (16 bytes) ──
	copy(buf[0:8], protocol.FlagV211)
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
			valueSize := protocol.TypeByteSize(item.dataType)
			switch item.dataType {
			case protocol.TypeBoolean, protocol.TypeBit:
				buf[pos] = byte(item.value & 1)
			case protocol.TypeInt8:
				buf[pos] = byte(int8(item.value))
			case protocol.TypeUint8:
				buf[pos] = byte(item.value)
			case protocol.TypeInt16:
				binary.BigEndian.PutUint16(buf[pos:pos+2], uint16(int16(item.value)))
			case protocol.TypeUint16:
				binary.BigEndian.PutUint16(buf[pos:pos+2], uint16(item.value))
			case protocol.TypeFloat16:
				// SHM stores float16 as float32 bit pattern; convert back to float16
				f16 := float32ToFloat16(uint32(item.value))
				binary.BigEndian.PutUint16(buf[pos:pos+2], f16)
			case protocol.TypeInt32:
				binary.BigEndian.PutUint32(buf[pos:pos+4], uint32(int32(item.value)))
			case protocol.TypeUint32:
				binary.BigEndian.PutUint32(buf[pos:pos+4], uint32(item.value))
			case protocol.TypeFloat32:
				// SHM stores float32 bit pattern in native byte order as uint64.
				// The uint32 numeric value contains the IEEE 754 bits in canonical form;
				// binary.BigEndian.PutUint32 writes them in BE (network) order — correct for v211.
				binary.BigEndian.PutUint32(buf[pos:pos+4], uint32(item.value))
			case protocol.TypeInt64, protocol.TypeUint64:
				binary.BigEndian.PutUint64(buf[pos:pos+8], item.value)
			case protocol.TypeFloat64:
				binary.BigEndian.PutUint64(buf[pos:pos+8], item.value)
			}
			pos += valueSize
		}
	}

	return buf
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
//  Send loop
// ──────────────────────────────────────────────

// writeFrame writes b under the write lock with a bounded deadline — a
// zombie peer with a full TCP buffer must not stall runReceiver past stop.
func (ist *instanceState) writeFrame(conn net.Conn, b []byte) error {
	ist.writeMu.Lock()
	defer ist.writeMu.Unlock()
	conn.SetWriteDeadline(time.Now().Add(time.Second))
	_, err := conn.Write(b)
	conn.SetWriteDeadline(time.Time{})
	return err
}

// markConnDead flags the link as broken so sendRound performs the reconnect
// (single reconnect owner) and records why. Only fires when conn is still the
// current one — a stale receiver must not kill a freshly re-established link.
// T0 从断链时刻起算：downAt 在此设置，sendRound 到期后才重拨（§T0）。
func (ist *instanceState) markConnDead(conn net.Conn, reason string) {
	ist.mu.Lock()
	same := ist.conn != nil && ist.conn == conn
	if same {
		ist.conn.Close()
		ist.conn = nil
		// 首个原因保留：t2_timeout 等权威判定不被后续读错误覆盖
		if ist.deadReason == "" {
			ist.deadReason = reason
		}
		ist.downAt = time.Now()
	}
	ist.mu.Unlock()
	if same {
		ist.connDead.Store(true)
		ist.log.Info("disconnected",
			"remote", fmt.Sprintf("%s:%d", ist.cfg.IP, ist.cfg.Port),
			"reason", reason,
			"duration_s", time.Since(ist.connectedAt).Seconds(),
			"reconnect_after_s", ist.t0.Seconds())
	}
}
// reconnectAfterError is the single reconnect path, invoked by sendRound once
// T0 has elapsed since the link went down (§T0): dials once; on failure the
// T0 window restarts so the next attempt waits a full period. Reuses the
// post-Dial quit re-check and the stopHandler wg.Wait backstop (§9.5).
func (ist *instanceState) reconnectAfterError() {
	remote := fmt.Sprintf("%s:%d", ist.cfg.IP, ist.cfg.Port)

	// Try reconnect only if not stopping
	select {
	case <-ist.quit:
		return
	default:
	}
	attempt := ist.connectAttempt.Add(1)
	newConn, dialErr := net.Dial("tcp", net.JoinHostPort(ist.cfg.IP, strconv.Itoa(ist.cfg.Port)))
	if dialErr == nil {
		// Dial 与 stop 竞争时在此补检 quit，避免把新 socket 挂到已停止的实例上
		select {
		case <-ist.quit:
			newConn.Close()
			return
		default:
		}
		elapsed := time.Since(ist.downAt).Milliseconds()
		ist.mu.Lock()
		ist.conn = newConn
		ist.deadReason = ""
		ist.downAt = time.Time{}
		ist.mu.Unlock()
		atomic.AddUint64(&ist.stats.reconnects, 1)
		ist.connectAttempt.Store(0)
		ist.connectedAt = time.Now()
		ist.connDead.Store(false)
		ist.connectWarn = time.Time{}
		ist.ka.resetT1()
		ist.ka.stopT2()
		ist.log.Info("connected", "remote", remote,
			"attempt", attempt, "elapsed_ms", elapsed)
	} else if ist.connectWarn.IsZero() || time.Since(ist.connectWarn) >= 60*time.Second {
		ist.connectWarn = time.Now()
		ist.log.Warn("connect_failed", "remote", remote,
			"attempt", attempt,
			"retry_after_s", ist.t0.Seconds(),
			"err", dialErr.Error())
		// 重拨失败：重置 T0 窗口，下个整周期再试（§T0）
		ist.mu.Lock()
		ist.downAt = time.Now()
		ist.mu.Unlock()
	}
}

func sendRound(ist *instanceState, shmData []byte) {
	// Link flagged dead (t2 timeout / recv error / initial-connect failure) —
	// reconnect only after T0 elapsed since the link went down (§T0, 图 814).
	if ist.connDead.Load() {
		ist.mu.Lock()
		due := time.Since(ist.downAt) >= ist.t0
		ist.mu.Unlock()
		if !due {
			return
		}
		ist.reconnectAfterError()
		return
	}

	// 1. Scan all configured shm_ids in addr-sorted order → read blocks via seqlock
	var items []shmItem
	for _, pt := range ist.sortedPts {
		shmID, addr := pt.shmID, pt.addr
		dt, ts, val, seq, ok := readBlock(shmData, shmID)
		if !ok {
			continue
		}
		// Skip non-numeric types
		if protocol.VariableTypes[dt] {
			atomic.AddUint64(&ist.stats.itemsSkipped, 1)
			if !ist.encodeSkippedLogged.Swap(true) {
				ist.log.Warn("encode_skipped", "key", ist.ptKeys[shmID], "shm_id", shmID,
					"addr", addr, "reason", "variable_length_type", "type", dt)
			}
			continue
		}

		// Only send if write_seq > last_seen
		lastSeq := ist.lastSeen[shmID]
		if seq <= lastSeq {
			atomic.AddUint64(&ist.stats.smartSkipped, 1)
			continue
		}

		items = append(items, shmItem{
			shmID:     shmID,
			addr:      addr,
			dataType:  dt,
			timestamp: ts,
			value:     val,
			seq:       seq,
		})
	}

	if len(items) == 0 {
		return
	}

	// 2. Split by addr continuity into subgroups
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

		if err := ist.writeFrame(conn, pkt); err != nil {
			// Connection broken — enter the T0 reconnect flow (§T0)
			atomic.AddUint64(&ist.stats.sendErrors, 1)
			ist.markConnDead(conn, "send_error")
			// If reconnect failed, skip remaining subgroups this round
			return
		}
		// Data sent resets the T1 idle timer (ASFP2 2.1.1)
		ist.ka.resetT1()
		atomic.AddUint64(&ist.stats.packetsSent, 1)
		atomic.AddUint64(&ist.stats.pointsSent, uint64(len(sg)))
	}

	// 5. Update last_seen
	for _, item := range items {
		ist.lastSeen[item.shmID] = item.seq
	}
}

func runSender(ist *instanceState, shmData []byte) {
	defer func() {
		if r := recover(); r != nil {
			ist.log.Error("panic_recovered", "goroutine", "runSender", "err", fmt.Sprint(r))
		}
	}()
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

// runReceiver reads inbound heartbeat frames from the current connection and
// drives the client-side keepalive state machine (ASFP2 2.1.1):
//   - 1 byte == forward_kack  → kack for our KEEP: stop T2, restart T1
//   - 1 byte != forward_kack  → server inverse keepalive: restart T1, reply "KACK"
//
// It never reconnects itself — on fatal errors it flags the link via
// markConnDead and sendRound performs the reconnect (single reconnect owner).
func runReceiver(ist *instanceState) {
	defer func() {
		if r := recover(); r != nil {
			ist.log.Error("panic_recovered", "goroutine", "runReceiver", "err", fmt.Sprint(r))
		}
	}()

	remote := fmt.Sprintf("%s:%d", ist.cfg.IP, ist.cfg.Port)
	var buf [1]byte
	var cur net.Conn

	for {
		select {
		case <-ist.quit:
			return
		default:
		}

		ist.mu.Lock()
		conn := ist.conn
		ist.mu.Unlock()
		if conn == nil {
			// Disconnected — wait for sendRound to re-establish the link
			select {
			case <-ist.quit:
				return
			case <-time.After(100 * time.Millisecond):
			}
			continue
		}
		if conn != cur {
			// New (or first) connection: T1 starts on connection establishment
			cur = conn
			ist.ka.resetT1()
			ist.ka.stopT2()
		}

		conn.SetReadDeadline(time.Now().Add(time.Second))
		n, err := conn.Read(buf[:])
		if ne, ok := err.(net.Error); ok && ne.Timeout() {
			switch ist.ka.poll() {
			case "t2":
				// T2 expired: no kack for our KEEP — link presumed dead
				ist.log.Warn("t2_timeout", "remote", remote)
				ist.markConnDead(cur, "t2_timeout")
			case "t1":
				// T1 expired: idle — send forward keepalive
				if werr := ist.writeFrame(cur, []byte("KEEP")); werr != nil {
					ist.markConnDead(cur, "send_error")
					continue
				}
				ist.ka.resetT1()
				ist.ka.armT2()
				ist.log.Debug("keepalive_exchanged", "dir", "send_keep")
			}
			continue
		}
		if err != nil {
			// Fatal read (EOF/RST/closed): flag for reconnect unless the conn
			// was already replaced by a fresh reconnect
			ist.markConnDead(conn, "recv_error")
			cur = nil
			continue
		}
		if n != 1 {
			continue
		}

		if buf[0] == ist.cfg.ForwardKack {
			// Kack for our keepalive: stop T2, restart T1
			ist.ka.stopT2()
			ist.ka.resetT1()
			ist.log.Debug("keepalive_exchanged", "dir", "recv_kack")
		} else {
			// Server inverse keepalive: restart T1, reply "KACK"
			ist.ka.resetT1()
			if werr := ist.writeFrame(cur, []byte("KACK")); werr == nil {
				ist.log.Debug("keepalive_exchanged", "dir", "recv_keep", "reply", "kack")
			} else {
				ist.markConnDead(cur, "send_error")
			}
		}
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
				"err", "shared memory header magic is invalid, forward path interrupted")
			ist.stop()
			ist.mu.Lock()
			if ist.conn != nil {
				ist.conn.Close()
			}
			ist.mu.Unlock()
			return
		}
		if !statsEnabled {
			continue
		}
		ist.log.Info("stats_periodic",
			"packets_sent", atomic.LoadUint64(&ist.stats.packetsSent),
			"points_sent", atomic.LoadUint64(&ist.stats.pointsSent),
			"smart_skipped", atomic.LoadUint64(&ist.stats.smartSkipped),
			"encode_skipped", atomic.LoadUint64(&ist.stats.itemsSkipped),
			"send_errors", atomic.LoadUint64(&ist.stats.sendErrors),
			"reconnects", atomic.LoadUint64(&ist.stats.reconnects),
		)
	}
}

// ──────────────────────────────────────────────
//  MCP Tool Handlers
// ──────────────────────────────────────────────

type ClientStartInput struct {
	InstanceID string `json:"instance_id" jsonschema:"required,ASFP2 client instance id (^c4_[a-zA-Z0-9]+$)"`
	ConfigPath string `json:"config_path" jsonschema:"required,absolute path to config.json"`
}

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
		return newError(fmt.Sprintf("INVALID_INSTANCE_ID: instance_id must match ^c4_[a-zA-Z0-9]+$, got '%s'", instanceID)), nil
	}
	configPath, _ := args["config_path"].(string)
	if configPath == "" {
		return newError("CONFIG_PATH_MISSING: config_path is required"), nil
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

	// Empty instances array is valid — start succeeds with no senders
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
		shmIDs := make(map[int]uint32)
		ptKeys := make(map[int]string)
		lastSeen := make(map[int]uint64)
		for _, pt := range cfg.Points {
			if pt.ShmID > 0 {
				shmIDs[pt.ShmID] = pt.Addr
				ptKeys[pt.ShmID] = pt.Key
				lastSeen[pt.ShmID] = 0
			}
		}

		for shmID := range shmIDs {
			off := shmID * shm.BlockSize
			if off+shm.BlockSize > len(shmData) {
				log.Warn("config_invalid", "inst", cfg.Name,
					"err", fmt.Sprintf("SHM_ID_NOT_ASSIGNED: shm_id %d exceeds shared memory range", shmID))
				lastErr = fmt.Sprintf("SHM_ID_NOT_ASSIGNED: instance '%s': shm_id %d exceeds shared memory range", cfg.Name, shmID)
				break
			}
		}
		if lastErr != "" {
			break
		}

		instID := cfg.ID
		if instID == "" {
			instID = cfg.Name
		}
		addr := net.JoinHostPort(cfg.IP, strconv.Itoa(cfg.Port))
		// T0（§T0，图 814）：首连失败不终止启动——实例以未连接态创建，
		// sendRound 按 t0 周期后台重拨，目标就绪后自动建链。
		conn, dialErr := net.Dial("tcp", addr)
		if dialErr != nil {
			log.Warn("connect_failed", "inst", instID, "remote", addr,
				"attempt", 1, "t0_s", cfg.T0, "err", dialErr.Error())
		}

		inst := &instanceState{
			cfg:         cfg,
			conn:        conn,
			quit:        make(chan struct{}),
			shmIDs:      shmIDs,
			ptKeys:      ptKeys,
			lastSeen:    lastSeen,
			ka:          newKAClock(cfg.T1, cfg.T2),
			t0:          time.Duration(cfg.T0) * time.Second,
			log:         log.With("inst", instID),
			connectedAt: time.Now(),
			downAt:      time.Now(),
		}
		if conn == nil {
			inst.connDead.Store(true)
		}

		for shmID, addr := range shmIDs {
			inst.sortedPts = append(inst.sortedPts, shmIDAddr{shmID: shmID, addr: addr})
		}
		sort.Slice(inst.sortedPts, func(i, j int) bool { return inst.sortedPts[i].addr < inst.sortedPts[j].addr })

		instancesState = append(instancesState, inst)
	}

	if lastErr != "" {
		for _, ist := range instancesState {
			if ist.conn != nil {
				ist.conn.Close()
			}
		}
		unix.Munmap(shmData)
		unix.Close(shmFd)
		return newError(lastErr), nil
	}

	for _, ist := range instancesState {
		ist.log.Info("instance_started", "target", fmt.Sprintf("%s:%d", ist.cfg.IP, ist.cfg.Port),
			"timer_ms", ist.cfg.Timer, "smart", ist.cfg.Smart, "points", len(ist.cfg.Points))
		ist.wg.Add(1)
		go func() {
			defer func() {
				if r := recover(); r != nil {
					ist.log.Error("panic_recovered", "goroutine", "stats", "err", fmt.Sprint(r))
				}
			}()
			defer ist.wg.Done()
			statsLoop(ist, shmData)
		}()
		ist.wg.Add(1)
		go func() {
			defer ist.wg.Done()
			runReceiver(ist)
		}()
		ist.wg.Add(1)
		go func() {
			defer ist.wg.Done()
			runSender(ist, shmData)
		}()
	}

	state.mu.Lock()
	state.shmData = shmData
	state.shmFd = shmFd
	state.instances = instancesState
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
		ist.stop()
		ist.mu.Lock()
		if ist.conn != nil {
			ist.conn.Close()
		}
		ist.mu.Unlock()
		ist.wg.Wait()
		// wg 退出后再兜底关闭一次：覆盖重连 Dial 成功与 stop 竞争的窗口
		ist.mu.Lock()
		if ist.conn != nil {
			ist.conn.Close()
		}
		ist.mu.Unlock()
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
	log = logger.Init("c4_asfp2_client")

	server := mcp.NewServer(
		&mcp.Implementation{Name: "c4_asfp2_client", Version: "0.1.0"},
		nil,
	)

	server.AddTool(
		&mcp.Tool{
			Name:        "start",
			Description: "Start ASFP2 client sender instances",
			InputSchema: json.RawMessage(`{"type":"object","properties":{"instance_id":{"type":"string"},"config_path":{"type":"string"}},"required":["instance_id","config_path"]}`),
		},
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

	if err := server.Run(context.Background(), &mcp.StdioTransport{}); err != nil {
		stdlog.Fatal(err)
	}
}
