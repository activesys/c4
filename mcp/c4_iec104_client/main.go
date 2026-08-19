package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math"
	"net"
	"os"
	"regexp"
	"sync"
	"sync/atomic"
	"time"
	"unsafe"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"golang.org/x/sys/unix"

	"c4/mcp/internal/protocol"
	"c4/mcp/internal/shm"
)

// ──────────────────────────────────────────────
//  IEC 104 APCI frame types
// ──────────────────────────────────────────────

const (
	frameI = iota
	frameS
	frameU
)

// U-frame function codes (format_u.function).
const (
	funcStartdtAct uint8 = 0x04
	funcStartdtCon uint8 = 0x08
	funcStopdtAct  uint8 = 0x10
	funcStopdtCon  uint8 = 0x20
	funcTestfrAct  uint8 = 0x40
	funcTestfrCon  uint8 = 0x80
)

// ASDU type identification codes supported by this client.
const (
	typeMspNa1 uint8 = 1
	typeMdpNa1 uint8 = 3
	typeMstNa1 uint8 = 5
	typeMmeNa1 uint8 = 9
	typeMmeNb1 uint8 = 11
	typeMmeNc1 uint8 = 13
	typeMitNa1 uint8 = 15
	typeMmeNd1 uint8 = 21
	typeMspTb1 uint8 = 30
	typeMdpTb1 uint8 = 31
	typeMstTb1 uint8 = 32
	typeMmeTd1 uint8 = 34
	typeMmeTe1 uint8 = 35
	typeMmeTf1 uint8 = 36
	typeMitTb1 uint8 = 37

	typeCicNa1 uint8 = 100 // C_IC_NA_1 总召唤
	typeCciNa1 uint8 = 101 // C_CI_NA_1 累计量召唤
)

// COT (cause of transmission) causes.
const (
	cotAct      uint8 = 6  // 激活
	cotActcon   uint8 = 7  // 激活确认
	cotDeactcon uint8 = 9  // 去激活确认
	cotActterm  uint8 = 10 // 激活终止
)

const seqModulus = 32768 // 15-bit sequence numbers (modules must equal 32768)

// ──────────────────────────────────────────────
//  Configuration types
// ──────────────────────────────────────────────

type iec104Point struct {
	ID    string `json:"id"`
	Addr  uint32 `json:"addr"`
	ShmID int    `json:"shm_id"`
}

type iec104Instance struct {
	Name               string        `json:"name"`
	ID                 string        `json:"id"`
	IP                 string        `json:"ip"`
	Port               int           `json:"port"`
	K                  int           `json:"k"`
	W                  int           `json:"w"`
	T0                 int           `json:"t0"`
	T1                 int           `json:"t1"`
	T2                 int           `json:"t2"`
	T3                 int           `json:"t3"`
	Modules            int           `json:"modules"`
	CommonAddress      int           `json:"common_address"`
	IoaSize            int           `json:"ioa_size"`
	DiscardCp56time2a  int           `json:"discard_cp56time2a"`
	IgnoreQds          int           `json:"ignore_qds"`
	ItTimer            int           `json:"it_timer"`
	GiTimer            int           `json:"gi_timer"`
	Points             []iec104Point `json:"points"`
}

// pointMapping maps an IOA to a shared-memory block id.
type pointMapping struct {
	ShmID int
}

// instanceState holds per-instance runtime state.
type instanceState struct {
	cfg    iec104Instance
	points map[uint32]pointMapping
	conn   net.Conn
	mu     sync.Mutex
	quit   chan struct{}
	wg     sync.WaitGroup
}

type iec104State struct {
	started   atomic.Bool
	instances []*instanceState
	mu        sync.Mutex
	shmData   []byte
	shmFd     int
}

var state = &iec104State{}

// ──────────────────────────────────────────────
//  Config loading
// ──────────────────────────────────────────────

func loadConfig(configPath string) ([]iec104Instance, error) {
	data, err := os.ReadFile(configPath)
	if err != nil {
		return nil, fmt.Errorf("CONFIG_PATH_MISSING: cannot read config file: %v", err)
	}

	var fullCfg map[string]any
	if err := json.Unmarshal(data, &fullCfg); err != nil {
		return nil, fmt.Errorf("CONFIG_PARSE_ERROR: failed to parse config JSON: %v", err)
	}

	section, ok := fullCfg["c4_iec104_client"]
	if !ok {
		return nil, fmt.Errorf("CONFIG_PARSE_ERROR: 'c4_iec104_client' section not found in config")
	}

	rawJSON, _ := json.Marshal(section)
	var instances []iec104Instance
	if err := json.Unmarshal(rawJSON, &instances); err != nil {
		return nil, fmt.Errorf("CONFIG_PARSE_ERROR: failed to parse 'c4_iec104_client' section: %v", err)
	}

	return instances, nil
}

// validateConfig checks instance-level and point-level fields in the required
// order: shm_id (SHM_ID_NOT_ASSIGNED) → instance t2/t1, ioa_size, modules
// (INVALID_CONFIG) → point addr range/duplicate (INVALID_POINT) → id/ip/port
// (INVALID_CONFIG).
func validateConfig(instances []iec104Instance) error {
	// 1. shm_id must be assigned before anything else (see start-handler ordering).
	for _, inst := range instances {
		for _, pt := range inst.Points {
			if pt.ShmID == 0 {
				return fmt.Errorf("SHM_ID_NOT_ASSIGNED: point '%s' has shm_id=0, must be assigned by c4_shm_manager first", pt.ID)
			}
		}
	}

	for _, inst := range instances {
		// 2. instance-level: t2 < t1, ioa_size ∈ {1,2,3}, modules == 32768.
		if inst.T2 >= inst.T1 {
			return fmt.Errorf("INVALID_CONFIG: instance '%s' t2(=%d) must be < t1(=%d)", inst.ID, inst.T2, inst.T1)
		}
		if inst.IoaSize != 1 && inst.IoaSize != 2 && inst.IoaSize != 3 {
			return fmt.Errorf("INVALID_CONFIG: instance '%s' has invalid ioa_size=%d (must be 1/2/3)", inst.ID, inst.IoaSize)
		}
		if inst.Modules != 32768 {
			return fmt.Errorf("INVALID_CONFIG: instance '%s' has invalid modules=%d (must be 32768)", inst.ID, inst.Modules)
		}

		// 3. point-level: addr range (per ioa_size) and uniqueness within instance.
		var maxAddr uint32
		switch inst.IoaSize {
		case 1:
			maxAddr = 0xFF
		case 2:
			maxAddr = 0xFFFF
		case 3:
			maxAddr = 0xFFFFFF
		}
		seen := make(map[uint32]string)
		for _, pt := range inst.Points {
			if pt.Addr > maxAddr {
				return fmt.Errorf("INVALID_POINT: point '%s' has invalid addr=%d", pt.ID, pt.Addr)
			}
			if prev, ok := seen[pt.Addr]; ok {
				return fmt.Errorf("INVALID_POINT: duplicate addr=%d for points '%s' and '%s'", pt.Addr, prev, pt.ID)
			}
			seen[pt.Addr] = pt.ID
		}

		// 4. instance-level: non-empty id/ip, valid port.
		if inst.ID == "" {
			return fmt.Errorf("INVALID_CONFIG: instance has empty id field")
		}
		if inst.IP == "" {
			return fmt.Errorf("INVALID_CONFIG: instance '%s' has empty ip field", inst.ID)
		}
		if inst.Port <= 0 || inst.Port > 65535 {
			return fmt.Errorf("INVALID_CONFIG: instance '%s' has invalid port %d", inst.ID, inst.Port)
		}
	}
	return nil
}

// ──────────────────────────────────────────────
//  Shared memory (O_RDWR)
// ──────────────────────────────────────────────

var instanceIDRe = regexp.MustCompile("^c4_[a-zA-Z0-9]+$")

func validateInstanceID(id string) bool {
	return instanceIDRe.MatchString(id)
}

func attachShm(instanceID string) ([]byte, int, error) {
	shmPath := "/dev/shm/" + instanceID
	fd, err := unix.Open(shmPath, unix.O_RDWR, 0)
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
	data, err := unix.Mmap(fd, 0, int(totalSize), unix.PROT_READ|unix.PROT_WRITE, unix.MAP_SHARED)
	if err != nil {
		unix.Close(fd)
		return nil, 0, fmt.Errorf("SHM_OPEN_FAILED: mmap failed: %v", err)
	}

	return data, fd, nil
}

// ──────────────────────────────────────────────
//  Seqlock write (§5.2)
// ──────────────────────────────────────────────

func writeValue(shmData []byte, off int, value uint64, valueSize int) {
	for i := 0; i < 8; i++ {
		shmData[off+i] = 0
	}
	var buf [8]byte
	binary.NativeEndian.PutUint64(buf[:], value)
	copy(shmData[off:off+valueSize], buf[:valueSize])
}

func writeBlock(shmData []byte, shmID int, dataType uint8, timestamp uint64, value uint64, valueSize int) {
	off := shmID * shm.BlockSize
	if off+shm.BlockSize > len(shmData) {
		return
	}

	if binary.NativeEndian.Uint32(shmData[off+shm.BlkOffMagic:]) != shm.Magic {
		return
	}

	if shmData[off+shm.BlkOffState] == 0 {
		shmData[off+shm.BlkOffState] = 1
		atomic.StoreUint64((*uint64)(unsafe.Pointer(&shmData[off+shm.BlkOffWriteSeq])), 0)
	}

	atomic.AddUint64((*uint64)(unsafe.Pointer(&shmData[off+shm.BlkOffWriteSeq])), 1)

	binary.NativeEndian.PutUint64(shmData[off+shm.BlkOffTimestamp:], timestamp)
	shmData[off+shm.BlkOffType] = dataType
	writeValue(shmData, off+shm.BlkOffValue, value, valueSize)

	atomic.AddUint64((*uint64)(unsafe.Pointer(&shmData[off+shm.BlkOffWriteSeq])), 1)
}

// valueByteSize returns the shm value field byte size for an ASFP2 data type.
func valueByteSize(dataType uint8) int {
	switch dataType {
	case protocol.TypeBoolean, protocol.TypeInt8, protocol.TypeUint8:
		return 1
	case protocol.TypeInt16:
		return 2
	case protocol.TypeInt32, protocol.TypeFloat32:
		return 4
	default:
		return 0
	}
}

// ──────────────────────────────────────────────
//  APCI encode/decode (§4.1)
// ──────────────────────────────────────────────

// classifyFrame inspects the 4-byte APCI control field and returns the frame
// type plus N(S)/N(R) (for I/S frames) or the function code (for U frames).
func classifyFrame(ctrl []byte) (typ int, ns, nr int, function uint8) {
	octet1 := ctrl[0]
	octet3 := ctrl[2]

	if octet1&0x01 == 0 && octet3&0x01 == 0 {
		ns = int((uint16(ctrl[1])<<8 | uint16(octet1)) >> 1)
		nr = int((uint16(ctrl[3])<<8 | uint16(octet3)) >> 1)
		return frameI, ns, nr, 0
	}
	if octet1 == 0x01 && octet3&0x01 == 0 {
		nr = int((uint16(ctrl[3])<<8 | uint16(octet3)) >> 1)
		return frameS, 0, nr, 0
	}
	function = octet1 & 0xFC
	return frameU, 0, 0, function
}

// buildIFrame encodes an I-format APDU carrying the given ASDU.
func buildIFrame(ns, nr int, asdu []byte) []byte {
	frame := make([]byte, 0, 6+len(asdu))
	frame = append(frame, 0x68, byte(4+len(asdu)))
	frame = append(frame, byte((ns<<1)&0xFE), byte(ns>>7), byte((nr<<1)&0xFE), byte(nr>>7))
	frame = append(frame, asdu...)
	return frame
}

// buildSFrame encodes an S-format (supervisory) APDU acknowledging N(R).
func buildSFrame(nr int) []byte {
	return []byte{0x68, 0x04, 0x01, 0x00, byte((nr << 1) & 0xFE), byte(nr >> 7)}
}

// buildUFrame encodes a U-format (control) APDU with the given function code.
func buildUFrame(function uint8) []byte {
	return []byte{0x68, 0x04, 0x03 | function, 0x00, 0x00, 0x00}
}

// readAPDU reads a full APDU frame: 0x68 | length | (length bytes).
func readAPDU(conn net.Conn) ([]byte, error) {
	var header [2]byte
	if _, err := io.ReadFull(conn, header[:]); err != nil {
		return nil, err
	}
	if header[0] != 0x68 {
		return nil, fmt.Errorf("invalid start byte 0x%02X (want 0x68)", header[0])
	}
	length := int(header[1])
	if length < 4 || length > 253 {
		return nil, fmt.Errorf("invalid APDU length %d", length)
	}
	body := make([]byte, length)
	if _, err := io.ReadFull(conn, body); err != nil {
		return nil, err
	}
	frame := make([]byte, 2+length)
	frame[0] = header[0]
	frame[1] = header[1]
	copy(frame[2:], body)
	return frame, nil
}

// seqDiff returns (a - b) mod 32768.
func seqDiff(a, b int) int {
	return (a - b + seqModulus) % seqModulus
}

// ──────────────────────────────────────────────
//  ASDU parsing (§4.2 / §4.3 / §4.4 / §4.5)
// ──────────────────────────────────────────────

// elementLen returns the info-element byte length (excluding IOA and any
// optional CP56Time2a timestamp) for a supported type id, or 0 if unsupported.
func elementLen(typeID uint8) int {
	switch typeID {
	case typeMspNa1, typeMspTb1:
		return 1 // siq
	case typeMdpNa1, typeMdpTb1:
		return 1 // diq
	case typeMstNa1, typeMstTb1:
		return 2 // vti + qds
	case typeMmeNa1, typeMmeTd1:
		return 3 // nva(2) + qds(1)
	case typeMmeNd1:
		return 2 // nva(2), no qds
	case typeMmeNb1, typeMmeTe1:
		return 3 // sva(2) + qds(1)
	case typeMmeNc1, typeMmeTf1:
		return 5 // value(4) + qds(1)
	case typeMitNa1, typeMitTb1:
		return 5 // counter(4) + sequence_notation(1)
	}
	return 0
}

// hasTimestamp reports whether the type id carries a 7-byte CP56Time2a.
func hasTimestamp(typeID uint8) bool {
	switch typeID {
	case typeMspTb1, typeMdpTb1, typeMstTb1, typeMmeTd1, typeMmeTe1, typeMmeTf1, typeMitTb1:
		return true
	}
	return false
}

// valueTypeFor returns the ASFP2 type and value byte size for a type id.
func valueTypeFor(typeID uint8) (uint8, int) {
	switch typeID {
	case typeMspNa1, typeMspTb1:
		return protocol.TypeBoolean, 1
	case typeMdpNa1, typeMdpTb1:
		return protocol.TypeUint8, 1
	case typeMstNa1, typeMstTb1:
		return protocol.TypeInt8, 1
	case typeMmeNa1, typeMmeNd1, typeMmeTd1:
		return protocol.TypeFloat32, 4
	case typeMmeNb1, typeMmeTe1:
		return protocol.TypeInt16, 2
	case typeMmeNc1, typeMmeTf1:
		return protocol.TypeFloat32, 4
	case typeMitNa1, typeMitTb1:
		return protocol.TypeInt32, 4
	}
	return 0, 0
}

// extractElement decodes the value and quality (IV) from the info element.
// valid is false when the type id is unsupported.
func extractElement(typeID uint8, buf []byte) (value uint64, iv bool, valid bool) {
	switch typeID {
	case typeMspNa1, typeMspTb1:
		siq := buf[0]
		value = uint64(siq & 0x01) // spi
		iv = siq&0x80 != 0
		valid = true
	case typeMdpNa1, typeMdpTb1:
		diq := buf[0]
		value = uint64(diq & 0x03) // dpi
		iv = diq&0x80 != 0
		valid = true
	case typeMstNa1, typeMstTb1:
		vti := buf[0]
		value = uint64(vti & 0x7F) // value, bit0-6
		iv = buf[1]&0x80 != 0      // qds
		valid = true
	case typeMmeNa1, typeMmeNd1, typeMmeTd1:
		nva := int16(binary.LittleEndian.Uint16(buf[0:2]))
		value = uint64(math.Float32bits(float32(nva) / 32768.0))
		if typeID == typeMmeNd1 {
			iv = false // no quality → always valid
			valid = true
		} else {
			iv = buf[2]&0x80 != 0
			valid = true
		}
	case typeMmeNb1, typeMmeTe1:
		sva := int16(binary.LittleEndian.Uint16(buf[0:2]))
		value = uint64(uint16(sva))
		iv = buf[2]&0x80 != 0
		valid = true
	case typeMmeNc1, typeMmeTf1:
		value = uint64(binary.LittleEndian.Uint32(buf[0:4]))
		iv = buf[4]&0x80 != 0
		valid = true
	case typeMitNa1, typeMitTb1:
		cr := int32(binary.LittleEndian.Uint32(buf[0:4]))
		value = uint64(uint32(cr))
		iv = buf[4]&0x80 != 0 // sequence_notation.iv
		valid = true
	}
	return value, iv, valid
}

// decodeCP56Time2a decodes a 7-byte CP56Time2a into Unix epoch milliseconds.
func decodeCP56Time2a(buf []byte) (int64, bool) {
	if len(buf) < 7 {
		return 0, false
	}
	ms := binary.LittleEndian.Uint16(buf[0:2])
	minutes := buf[2] & 0x7F // clear IV bit
	hours := buf[3] & 0x7F   // clear SU bit
	dom := buf[4] & 0x1F     // day of month (low 5 bits)
	months := buf[5]
	years := buf[6]

	if years > 99 {
		return 0, false
	}
	if months < 1 || months > 12 {
		return 0, false
	}
	if dom < 1 || dom > 31 {
		return 0, false
	}
	if minutes > 59 || hours > 23 {
		return 0, false
	}

	t := time.Date(2000+int(years), time.Month(months), int(dom),
		int(hours), int(minutes), 0, int(ms)*int(time.Millisecond), time.Local)
	return t.UnixMilli(), true
}

// readIOA extracts an ioa_size-byte little-endian information object address.
func readIOA(buf []byte, ioaSize int) uint32 {
	switch ioaSize {
	case 1:
		return uint32(buf[0])
	case 2:
		return uint32(binary.LittleEndian.Uint16(buf[0:2]))
	default:
		var b [4]byte
		copy(b[:3], buf[0:3])
		return binary.LittleEndian.Uint32(b[:])
	}
}

// buildGIRequest constructs the C_IC_NA_1 (general interrogation) ASDU.
func buildGIRequest(commonAddress int) []byte {
	ca := uint16(commonAddress)
	asdu := make([]byte, 0, 10)
	asdu = append(asdu, typeCicNa1)        // type_id
	asdu = append(asdu, 0x01)              // VSQ: number=1, sq=0
	asdu = append(asdu, cotAct, 0x00)      // COT: cause=6, originator=0
	asdu = append(asdu, byte(ca), byte(ca>>8)) // CASDU (2B little endian)
	asdu = append(asdu, 0x00, 0x00, 0x00)  // IOA = 0 (3B little endian)
	asdu = append(asdu, 20)                // QOI = 20 (station interrogation)
	return asdu
}

// buildITRequest constructs the C_CI_NA_1 (counter interrogation) ASDU.
func buildITRequest(commonAddress int) []byte {
	ca := uint16(commonAddress)
	asdu := make([]byte, 0, 10)
	asdu = append(asdu, typeCciNa1)        // type_id
	asdu = append(asdu, 0x01)              // VSQ: number=1, sq=0
	asdu = append(asdu, cotAct, 0x00)      // COT: cause=6, originator=0
	asdu = append(asdu, byte(ca), byte(ca>>8)) // CASDU (2B little endian)
	asdu = append(asdu, 0x00, 0x00, 0x00)  // IOA = 0 (3B little endian)
	asdu = append(asdu, 0x45)              // QCC = freeze+no-reset, total request
	return asdu
}

// ──────────────────────────────────────────────
//  Connection / session management
// ──────────────────────────────────────────────

func (ist *instanceState) address() string {
	return fmt.Sprintf("%s:%d", ist.cfg.IP, ist.cfg.Port)
}

func (ist *instanceState) getConn() net.Conn {
	ist.mu.Lock()
	defer ist.mu.Unlock()
	return ist.conn
}

func (ist *instanceState) setConn(conn net.Conn) {
	ist.mu.Lock()
	defer ist.mu.Unlock()
	ist.conn = conn
}

func (ist *instanceState) closeConn() {
	ist.mu.Lock()
	defer ist.mu.Unlock()
	if ist.conn != nil {
		ist.conn.Close()
		ist.conn = nil
	}
}

var errStopped = errors.New("stopped")

// dialInterruptible dials with a timeout but returns early if quit closes.
func dialInterruptible(addr string, timeout time.Duration, quit <-chan struct{}) (net.Conn, error) {
	type result struct {
		conn net.Conn
		err  error
	}
	ch := make(chan result, 1)
	go func() {
		c, err := net.DialTimeout("tcp", addr, timeout)
		ch <- result{c, err}
	}()

	select {
	case r := <-ch:
		return r.conn, r.err
	case <-quit:
		go func() {
			if r := <-ch; r.conn != nil {
				r.conn.Close()
			}
		}()
		return nil, errStopped
	}
}

// session carries the per-connection protocol state for one instance.
type session struct {
	ist     *instanceState
	conn    net.Conn
	shmData []byte

	vs  int // V(S) — next send sequence number
	vr  int // V(R) — next expected receive sequence number
	ack int // peer's N(R) — last acknowledged send sequence

	framesSinceAck int  // I frames received since last S-frame ack
	waitingAck     bool // an I frame / TESTFR is awaiting confirmation (t1 armed)

	giActive bool
	itActive bool
}

// sendFrame writes a raw APDU and reports success.
func (s *session) sendFrame(frame []byte) bool {
	if _, err := s.conn.Write(frame); err != nil {
		return false
	}
	return true
}

// sendIFrame encodes and sends an I frame, bumping V(S) and arming t1.
func (s *session) sendIFrame(asdu []byte) bool {
	frame := buildIFrame(s.vs, s.vr, asdu)
	if !s.sendFrame(frame) {
		return false
	}
	s.vs = (s.vs + 1) % seqModulus
	s.waitingAck = true
	return true
}

// handleFrame processes one received APDU. Returns false on link error
// (caller reconnects).
func (s *session) handleFrame(frame []byte) bool {
	if len(frame) < 6 {
		return true
	}
	typ, ns, nr, function := classifyFrame(frame[2:6])

	switch typ {
	case frameU:
		switch function {
		case funcTestfrAct:
			// Reply TESTFR CON to keep the link alive.
			s.sendFrame(buildUFrame(funcTestfrCon))
		case funcTestfrCon:
			s.waitingAck = false
		case funcStopdtAct:
			s.sendFrame(buildUFrame(funcStopdtCon))
		}
		return true

	case frameS:
		if seqDiff(nr, s.ack) > 0 {
			s.ack = nr
		}
		if s.vs == s.ack {
			s.waitingAck = false
		}
		return true

	case frameI:
		if ns != s.vr {
			// Out-of-order I frame → link error.
			return false
		}
		s.vr = (s.vr + 1) % seqModulus
		if seqDiff(nr, s.ack) > 0 {
			s.ack = nr
		}
		if s.vs == s.ack {
			s.waitingAck = false
		}

		s.handleASDU(frame[6:])

		// Acknowledge per w threshold (t2 timer handles the timeout case).
		s.framesSinceAck++
		if s.ist.cfg.W > 0 && s.framesSinceAck >= s.ist.cfg.W {
			s.sendFrame(buildSFrame(s.vr))
			s.framesSinceAck = 0
		}
		return true
	}
	return true
}

// handleASDU parses an ASDU and writes matched points into shared memory.
func (s *session) handleASDU(asdu []byte) {
	if len(asdu) < 6 {
		return
	}
	typeID := asdu[0]
	vsq := asdu[1]
	number := int(vsq & 0x7F)
	sq := vsq&0x80 != 0
	cause := asdu[2] & 0x3F
	casdu := binary.LittleEndian.Uint16(asdu[4:6])

	// Common address must match; mismatched frames are dropped.
	if int(casdu) != s.ist.cfg.CommonAddress {
		return
	}

	// Track GI/IT summon termination to release mutual exclusion.
	// Both activation termination (ACTTERM) and deactivation confirmation
	// (DEACTCON) end a summon — mirrors libplugin104 (function.c).
	if cause == cotActterm || cause == cotDeactcon {
		if typeID == typeCicNa1 {
			s.giActive = false
		}
		if typeID == typeCciNa1 {
			s.itActive = false
		}
	}

	elLen := elementLen(typeID)
	if elLen == 0 {
		return // unsupported type id — skip the whole ASDU
	}
	hasTS := hasTimestamp(typeID)
	valueType, valueSize := valueTypeFor(typeID)

	off := 6
	ioa := uint32(0)
	now := time.Now().UnixMilli()
	ignoreQds := s.ist.cfg.IgnoreQds == 1
	discardTS := s.ist.cfg.DiscardCp56time2a == 1

	for i := 0; i < number; i++ {
		if !sq || i == 0 {
			if off+s.ist.cfg.IoaSize > len(asdu) {
				return
			}
			ioa = readIOA(asdu[off:], s.ist.cfg.IoaSize)
			off += s.ist.cfg.IoaSize
		} else {
			ioa++
		}

		if off+elLen > len(asdu) {
			return
		}
		elem := asdu[off : off+elLen]
		off += elLen

		pm, found := s.ist.points[ioa]
		if !found {
			if hasTS {
				off += 7
			}
			continue
		}

		value, iv, ok := extractElement(typeID, elem)
		if !ok {
			continue
		}

		// Quality: skip invalid points unless ignore_qds is set. M_ME_ND_1 is
		// always valid (extractElement reports iv=false for it).
		if iv && !ignoreQds {
			if hasTS {
				off += 7
			}
			continue
		}

		var timestamp uint64
		if discardTS || !hasTS {
			timestamp = uint64(now)
		} else {
			ts, ok := decodeCP56Time2a(asdu[off : off+7])
			if !ok {
				timestamp = uint64(now)
			} else {
				timestamp = uint64(ts)
			}
			off += 7
		}

		writeBlock(s.shmData, pm.ShmID, valueType, timestamp, value, valueSize)
	}
}

// startdt performs the STARTDT handshake. Returns true on activation.
func (s *session) startdt(t1 time.Duration) bool {
	if !s.sendFrame(buildUFrame(funcStartdtAct)) {
		return false
	}
	if err := s.conn.SetReadDeadline(time.Now().Add(t1)); err != nil {
		return false
	}
	for {
		frame, err := readAPDU(s.conn)
		if err != nil {
			return false
		}
		if len(frame) < 6 {
			continue
		}
		typ, _, _, function := classifyFrame(frame[2:6])
		if typ == frameU && function == funcStartdtCon {
			return true
		}
	}
}

// runSession drives one connection lifecycle: STARTDT + receive loop + timers.
func runSession(ist *instanceState, conn net.Conn, shmData []byte) {
	s := &session{
		ist:     ist,
		conn:    conn,
		shmData: shmData,
		vs:      0,
		vr:      0,
		ack:     0,
	}
	ist.setConn(conn)
	defer ist.closeConn()

	t1 := time.Duration(ist.cfg.T1) * time.Second
	if !s.startdt(t1) {
		log.Printf("[iec104] instance '%s' STARTDT activation failed, reconnecting", ist.cfg.ID)
		return
	}

	// Clear read deadline for the receive loop.
	_ = conn.SetReadDeadline(time.Time{})

	// Frame reader goroutine → channel.
	frames := make(chan []byte, 512)
	readDone := make(chan error, 1)
	go func() {
		for {
			frame, err := readAPDU(conn)
			if err != nil {
				readDone <- err
				return
			}
			select {
			case frames <- frame:
			case <-ist.quit:
				return
			}
		}
	}()

	// GI / IT tickers.
	var giCh, itCh <-chan time.Time
	var giTicker, itTicker *time.Ticker
	if ist.cfg.GiTimer > 0 {
		giTicker = time.NewTicker(time.Duration(ist.cfg.GiTimer) * time.Millisecond)
		giCh = giTicker.C
	}
	if ist.cfg.ItTimer > 0 {
		itTicker = time.NewTicker(time.Duration(ist.cfg.ItTimer) * time.Millisecond)
		itCh = itTicker.C
	}
	defer func() {
		if giTicker != nil {
			giTicker.Stop()
		}
		if itTicker != nil {
			itTicker.Stop()
		}
	}()

	// t2: send S-frame ack if I frames are pending but fewer than w.
	var t2Timer *time.Timer
	var t2Ch <-chan time.Time
	if ist.cfg.T2 > 0 {
		t2Timer = time.NewTimer(time.Duration(ist.cfg.T2) * time.Second)
		t2Ch = t2Timer.C
		defer t2Timer.Stop()
	}

	// t3: idle watchdog — send TESTFR ACT after t3 of inactivity.
	t3Period := time.Duration(ist.cfg.T3) * time.Second
	lastActivity := time.Now()
	var t3Ticker *time.Ticker
	var t3Ch <-chan time.Time
	if ist.cfg.T3 > 0 {
		t3Ticker = time.NewTicker(time.Second)
		t3Ch = t3Ticker.C
		defer t3Ticker.Stop()
	}

	// t1: send confirmation timeout.
	t1Timer := time.NewTimer(t1)
	t1Timer.Stop()
	defer t1Timer.Stop()

	armT1 := func() {
		if !t1Timer.Stop() {
			select {
			case <-t1Timer.C:
			default:
			}
		}
		t1Timer.Reset(t1)
	}

	stopT1 := func() {
		if !t1Timer.Stop() {
			select {
			case <-t1Timer.C:
			default:
			}
		}
	}

	giDeadline := time.Time{}
	itDeadline := time.Time{}

	for {
		select {
		case <-ist.quit:
			// stopHandler already sent STOPDT ACT (best-effort) and waits for
			// the controlled station to reply before closing the connection.
			return

		case err := <-readDone:
			if err != nil {
				log.Printf("[iec104] instance '%s' read error: %v, reconnecting", ist.cfg.ID, err)
			}
			return

		case frame := <-frames:
			lastActivity = time.Now()
			if !s.handleFrame(frame) {
				log.Printf("[iec104] instance '%s' sequence error, reconnecting", ist.cfg.ID)
				return
			}
			// Reset t2 whenever an I frame arrives (per §4.6).
			if t2Timer != nil {
				if !t2Timer.Stop() {
					select {
					case <-t2Timer.C:
					default:
					}
				}
				t2Timer.Reset(time.Duration(ist.cfg.T2) * time.Second)
			}

		case now := <-giCh:
			if s.giActive && now.After(giDeadline) {
				s.giActive = false
			}
			if s.giActive {
				continue
			}
			if s.itActive || seqDiff(s.vs, s.ack) >= ist.cfg.K {
				// IT in progress or send window full — retry shortly so GI and IT
				// alternate instead of GI starving IT (whose tick fires at the
				// same instant).
				giTicker.Reset(10 * time.Millisecond)
				continue
			}
			if s.sendIFrame(buildGIRequest(ist.cfg.CommonAddress)) {
				s.giActive = true
				giDeadline = now.Add(time.Duration(ist.cfg.GiTimer) * 2 * time.Millisecond)
				armT1()
				giTicker.Reset(time.Duration(ist.cfg.GiTimer) * time.Millisecond)
			}

		case now := <-itCh:
			if s.itActive && now.After(itDeadline) {
				s.itActive = false
			}
			if s.itActive {
				continue
			}
			if s.giActive || seqDiff(s.vs, s.ack) >= ist.cfg.K {
				itTicker.Reset(10 * time.Millisecond)
				continue
			}
			if s.sendIFrame(buildITRequest(ist.cfg.CommonAddress)) {
				s.itActive = true
				itDeadline = now.Add(time.Duration(ist.cfg.ItTimer) * 2 * time.Millisecond)
				armT1()
				itTicker.Reset(time.Duration(ist.cfg.ItTimer) * time.Millisecond)
			}

		case <-t2Ch:
			if s.framesSinceAck > 0 {
				s.sendFrame(buildSFrame(s.vr))
				s.framesSinceAck = 0
			}

		case now := <-t3Ch:
			if s.waitingAck {
				lastActivity = now
				continue
			}
			if now.Sub(lastActivity) >= t3Period {
				if s.sendFrame(buildUFrame(funcTestfrAct)) {
					s.waitingAck = true
					lastActivity = now
					armT1()
				}
			}

		case <-t1Timer.C:
			if s.waitingAck {
				log.Printf("[iec104] instance '%s' t1 timeout (unconfirmed send), reconnecting", ist.cfg.ID)
				return
			}
		}

		// Keep t1 off once nothing is awaiting confirmation.
		if !s.waitingAck {
			stopT1()
		}
	}
}

// runInstance connects, hands off to runSession, and reconnects on failure.
func runInstance(ist *instanceState, shmData []byte) {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("runInstance panic recovered: %v", r)
		}
	}()
	defer ist.wg.Done()

	t0 := time.Duration(ist.cfg.T0) * time.Second

	for {
		select {
		case <-ist.quit:
			return
		default:
		}

		conn, err := dialInterruptible(ist.address(), t0, ist.quit)
		if err != nil {
			if err == errStopped {
				return
			}
			log.Printf("[iec104] instance '%s' connect to %s failed: %v, retrying in %v", ist.cfg.ID, ist.address(), err, t0)
			select {
			case <-ist.quit:
				return
			case <-time.After(t0):
			}
			continue
		}

		runSession(ist, conn, shmData)

		select {
		case <-ist.quit:
			return
		case <-time.After(t0):
		}
	}
}

// ──────────────────────────────────────────────
//  MCP Tool Handlers
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
		points := make(map[uint32]pointMapping, len(cfg.Points))
		for _, pt := range cfg.Points {
			points[pt.Addr] = pointMapping{ShmID: pt.ShmID}
		}

		ist := &instanceState{
			cfg:    cfg,
			points: points,
			quit:   make(chan struct{}),
		}
		instancesState = append(instancesState, ist)
	}

	for _, ist := range instancesState {
		ist.wg.Add(1)
		go func(ist *instanceState) {
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
		// Best-effort STOPDT before closing.
		if c := ist.getConn(); c != nil {
			c.SetWriteDeadline(time.Now().Add(500 * time.Millisecond))
			c.Write(buildUFrame(funcStopdtAct))
			c.SetWriteDeadline(time.Time{})
		}
		close(ist.quit)
		// Give the controlled station a short window to reply STOPDT/STARTDT CON
		// before we close the TCP connection. Closing immediately after sending
		// STOPDT ACT causes iec104d to write CON to a closed socket and die of
		// SIGPIPE (iec104d does not ignore SIGPIPE).
		time.Sleep(300 * time.Millisecond)
		ist.closeConn()
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
		&mcp.Implementation{Name: "c4_iec104_client", Version: "0.1.0"},
		nil,
	)

	server.AddTool(
		&mcp.Tool{
			Name:        "start",
			Description: "Start IEC 60870-5-104 client (Controlling Station) instances",
			InputSchema: json.RawMessage(`{"type":"object","properties":{"instance_id":{"type":"string"},"config_path":{"type":"string"}},"required":["instance_id","config_path"]}`),
		},
		startHandler,
	)

	mcp.AddTool(server,
		&mcp.Tool{
			Name:        "stop",
			Description: "Stop all IEC 104 client instances and release resources",
			InputSchema: json.RawMessage(`{"type":"object","properties":{},"required":[]}`),
		},
		stopHandler,
	)

	if err := server.Run(context.Background(), &mcp.StdioTransport{}); err != nil {
		log.Fatal(err)
	}
}
