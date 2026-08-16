package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"regexp"
	"sort"
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
//  Configuration types
// ──────────────────────────────────────────────

type modbusPoint struct {
	ID    string `json:"id"`
	UID   int    `json:"uid"`
	Addr  uint32 `json:"addr"`
	Fun   uint8  `json:"fun"`
	Type  uint8  `json:"type"`
	Swap  int    `json:"swap"`
	ShmID int    `json:"shm_id"`
}

type modbusInstance struct {
	Name                 string        `json:"name"`
	ID                   string        `json:"id"`
	IP                   string        `json:"ip"`
	Port                 int           `json:"port"`
	T0                   int           `json:"t0"`
	T1                   int           `json:"t1"`
	Retries              int           `json:"retries"`
	CoilsQuantityMax     int           `json:"coils_quantity_max"`
	RegistersQuantityMax int           `json:"registers_quantity_max"`
	HtonRegister         int           `json:"hton_register"`
	HtonTotal            int           `json:"hton_total"`
	Timer                int           `json:"timer"`
	Points               []modbusPoint `json:"points"`
}

// ──────────────────────────────────────────────
//  Mapping index
// ──────────────────────────────────────────────

type pointMapping struct {
	UID   uint8
	Fun   uint8
	Addr  uint32
	ShmID int
	Type  uint8
	Span  uint8
	Swap  int
}

type modbusAddr struct {
	UID  uint8
	Fun  uint8
	Addr uint32
}

// batch is a contiguous run of points within the same (uid, fun) group.
type batch struct {
	UID    uint8
	Fun    uint8
	Addr   uint32
	Qty    uint16
	Points []*pointMapping
}

// ──────────────────────────────────────────────
//  Instance / global state
// ──────────────────────────────────────────────

type instanceStats struct {
	pollErrors   uint64
	reconnects   uint64
	itemsWritten uint64
	itemsDropped uint64
}

type instanceState struct {
	cfg     modbusInstance
	conn    net.Conn
	mu      sync.Mutex
	batches []*batch
	txID    uint32
	quit    chan struct{}
	wg      sync.WaitGroup
	stats   instanceStats
}

type modbusState struct {
	started   atomic.Bool
	instances []*instanceState
	mu        sync.Mutex
	shmData   []byte
	shmFd     int
}

var state = &modbusState{}

// ──────────────────────────────────────────────
//  Config loading
// ──────────────────────────────────────────────

func loadConfig(configPath string) ([]modbusInstance, error) {
	data, err := os.ReadFile(configPath)
	if err != nil {
		return nil, fmt.Errorf("CONFIG_PATH_MISSING: cannot read config file: %v", err)
	}

	var fullCfg map[string]any
	if err := json.Unmarshal(data, &fullCfg); err != nil {
		return nil, fmt.Errorf("CONFIG_PARSE_ERROR: failed to parse config JSON: %v", err)
	}

	section, ok := fullCfg["c4_modbus_client"]
	if !ok {
		return nil, fmt.Errorf("CONFIG_PARSE_ERROR: 'c4_modbus_client' section not found in config")
	}

	rawJSON, _ := json.Marshal(section)
	var instances []modbusInstance
	if err := json.Unmarshal(rawJSON, &instances); err != nil {
		return nil, fmt.Errorf("CONFIG_PARSE_ERROR: failed to parse 'c4_modbus_client' section: %v", err)
	}

	return instances, nil
}

// pointSpan returns the number of data units a point occupies:
// coil/DI point = 1 bit; register point = 1/2/4 registers.
func pointSpan(fun uint8, dataType uint8) uint8 {
	if fun == 1 || fun == 2 {
		return 1
	}
	switch dataType {
	case protocol.TypeInt16, protocol.TypeUint16:
		return 1
	case protocol.TypeInt32, protocol.TypeUint32, protocol.TypeFloat32:
		return 2
	case protocol.TypeInt64, protocol.TypeUint64, protocol.TypeFloat64:
		return 4
	default:
		return 0
	}
}

// valueByteSize returns the shm value field byte size for a data type.
func valueByteSize(dataType uint8) int {
	switch dataType {
	case protocol.TypeBoolean, protocol.TypeBit:
		return 1
	case protocol.TypeInt16, protocol.TypeUint16:
		return 2
	case protocol.TypeInt32, protocol.TypeUint32, protocol.TypeFloat32:
		return 4
	case protocol.TypeInt64, protocol.TypeUint64, protocol.TypeFloat64:
		return 8
	default:
		return 0
	}
}

// validTypeForFun reports whether dataType is legal for a function code.
func validTypeForFun(fun uint8, dataType uint8) bool {
	if fun == 1 || fun == 2 {
		return dataType == protocol.TypeBoolean || dataType == protocol.TypeBit
	}
	if fun == 3 || fun == 4 {
		switch dataType {
		case protocol.TypeInt16, protocol.TypeUint16,
			protocol.TypeInt32, protocol.TypeUint32,
			protocol.TypeInt64, protocol.TypeUint64,
			protocol.TypeFloat32, protocol.TypeFloat64:
			return true
		}
	}
	return false
}

func validateConfig(instances []modbusInstance) error {
	for _, inst := range instances {
		if inst.ID == "" {
			return fmt.Errorf("CONFIG_PARSE_ERROR: instance has empty id field")
		}
		if inst.IP == "" {
			return fmt.Errorf("CONFIG_PARSE_ERROR: instance '%s' has empty ip field", inst.ID)
		}
		if inst.Port <= 0 || inst.Port > 65535 {
			return fmt.Errorf("CONFIG_PARSE_ERROR: instance '%s' has invalid port %d", inst.ID, inst.Port)
		}

		seen := make(map[modbusAddr]string)
		groups := make(map[[2]int][]modbusPoint)
		for _, pt := range inst.Points {
			if pt.ShmID == 0 {
				return fmt.Errorf("SHM_ID_NOT_ASSIGNED: point '%s' has shm_id=0, must be assigned by c4_shm_manager first", pt.ID)
			}
			if pt.UID < 0 || pt.UID > 255 {
				return fmt.Errorf("INVALID_POINT: point '%s' has invalid uid=%d (must be 0~255)", pt.ID, pt.UID)
			}
			if pt.Addr > 0xFFFF {
				return fmt.Errorf("INVALID_POINT: point '%s' has invalid addr=%d (must be 0~65535)", pt.ID, pt.Addr)
			}
			if pt.Fun != 1 && pt.Fun != 2 && pt.Fun != 3 && pt.Fun != 4 {
				return fmt.Errorf("INVALID_POINT: point '%s' has invalid fun=%d (must be 1/2/3/4)", pt.ID, pt.Fun)
			}
			if !validTypeForFun(pt.Fun, pt.Type) {
				return fmt.Errorf("INVALID_POINT: point '%s' has invalid type=%d for fun=%d", pt.ID, pt.Type, pt.Fun)
			}

			span := int(pointSpan(pt.Fun, pt.Type))
			byteCount := span * 2
			if pt.Fun == 1 || pt.Fun == 2 {
				byteCount = 1
			}
			if pt.Swap != 0 && pt.Swap != 1 && pt.Swap != 2 && pt.Swap != 4 {
				return fmt.Errorf("INVALID_POINT: point '%s' has invalid swap=%d (must be 0/1/2/4)", pt.ID, pt.Swap)
			}
			if byteCount == 1 || byteCount == 2 {
				if pt.Swap != 0 {
					return fmt.Errorf("INVALID_POINT: point '%s' type=%d is single-unit, swap must be 0 (got %d)", pt.ID, pt.Type, pt.Swap)
				}
			} else if pt.Swap > 0 && byteCount%pt.Swap != 0 {
				return fmt.Errorf("INVALID_POINT: point '%s' swap=%d does not divide byte count %d", pt.ID, pt.Swap, byteCount)
			}

			key := modbusAddr{UID: uint8(pt.UID), Fun: pt.Fun, Addr: pt.Addr}
			if prev, ok := seen[key]; ok {
				return fmt.Errorf("INVALID_POINT: duplicate (uid=%d,fun=%d,addr=%d) for points '%s' and '%s'", pt.UID, pt.Fun, pt.Addr, prev, pt.ID)
			}
			seen[key] = pt.ID

			gkey := [2]int{pt.UID, int(pt.Fun)}
			groups[gkey] = append(groups[gkey], pt)
		}

		for gkey, pts := range groups {
			sort.Slice(pts, func(i, j int) bool { return pts[i].Addr < pts[j].Addr })
			for i := 1; i < len(pts); i++ {
				prev := pts[i-1]
				prevSpan := pointSpan(uint8(gkey[1]), prev.Type)
				if pts[i].Addr < prev.Addr+uint32(prevSpan) {
					return fmt.Errorf("INVALID_POINT: point '%s' addr=%d overlaps point '%s' (addr=%d span=%d) in group (uid=%d,fun=%d)", pts[i].ID, pts[i].Addr, prev.ID, prev.Addr, prevSpan, gkey[0], gkey[1])
				}
			}
		}
	}
	return nil
}

// ──────────────────────────────────────────────
//  Shared memory (O_RDWR)
// ──────────────────────────────────────────────

// instanceIDRe is the valid instance_id / shm name pattern (see docs/design/c4_modbus_client.md §6.1).
var instanceIDRe = regexp.MustCompile("^c4_[a-zA-Z0-9]+$")

// validateInstanceID reports whether id is a legal shm name for a modbus client instance.
func validateInstanceID(id string) bool {
	return instanceIDRe.MatchString(id)
}

// attachShm opens the POSIX shm segment for instanceID directly by name.
// The modbus client is a Writer, so the segment is opened with O_RDWR.
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
//  Batch building (§4.5)
// ──────────────────────────────────────────────

func buildBatches(points []*pointMapping, coilsMax, regsMax int) []*batch {
	groups := make(map[[2]int][]*pointMapping)
	for _, pt := range points {
		key := [2]int{int(pt.UID), int(pt.Fun)}
		groups[key] = append(groups[key], pt)
	}

	var batches []*batch
	for _, group := range groups {
		sort.Slice(group, func(i, j int) bool { return group[i].Addr < group[j].Addr })

		var cur []*pointMapping
		var curSpan uint32
		var curAddr uint32
		flush := func() {
			if len(cur) > 0 {
				batches = append(batches, &batch{
					UID:    cur[0].UID,
					Fun:    cur[0].Fun,
					Addr:   curAddr,
					Qty:    uint16(curSpan),
					Points: cur,
				})
			}
		}

		for _, pt := range group {
			if len(cur) == 0 {
				cur = []*pointMapping{pt}
				curSpan = uint32(pt.Span)
				curAddr = pt.Addr
				continue
			}
			limit := coilsMax
			if pt.Fun == 3 || pt.Fun == 4 {
				limit = regsMax
			}
			if pt.Addr == curAddr+curSpan && int(curSpan+uint32(pt.Span)) <= limit {
				cur = append(cur, pt)
				curSpan += uint32(pt.Span)
				continue
			}
			flush()
			cur = []*pointMapping{pt}
			curSpan = uint32(pt.Span)
			curAddr = pt.Addr
		}
		flush()
	}
	return batches
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

// ──────────────────────────────────────────────
//  Byte-order decode (§4.6)
// ──────────────────────────────────────────────

// swapByte mirrors swap-byte groups (first↔last) per the _swap_byte semantics.
func swapByte(buf []byte, swap int) {
	count := len(buf)
	if swap == 0 || swap >= count {
		return
	}
	for i := 0; i < count/swap/2; i++ {
		for j := 0; j < swap; j++ {
			a := i*swap + j
			b := count - (i+1)*swap + j
			buf[a], buf[b] = buf[b], buf[a]
		}
	}
}

func interpretValue(buf []byte, dataType uint8) uint64 {
	switch dataType {
	case protocol.TypeInt16:
		return uint64(uint16(int16(binary.NativeEndian.Uint16(buf))))
	case protocol.TypeUint16:
		return uint64(binary.NativeEndian.Uint16(buf))
	case protocol.TypeInt32:
		return uint64(uint32(int32(binary.NativeEndian.Uint32(buf))))
	case protocol.TypeUint32:
		return uint64(binary.NativeEndian.Uint32(buf))
	case protocol.TypeInt64, protocol.TypeUint64:
		return binary.NativeEndian.Uint64(buf)
	case protocol.TypeFloat32:
		return uint64(binary.NativeEndian.Uint32(buf))
	case protocol.TypeFloat64:
		return binary.NativeEndian.Uint64(buf)
	}
	return 0
}

// decodeRegisterPoint extracts and decodes a register point's value from the
// response data buffer (which holds register values in network order).
func decodeRegisterPoint(data []byte, batchAddr uint32, pt *pointMapping, htonRegister int) uint64 {
	regOffset := int(pt.Addr-batchAddr) * 2
	raw := make([]byte, int(pt.Span)*2)
	copy(raw, data[regOffset:regOffset+int(pt.Span)*2])

	if htonRegister == 1 {
		for i := 0; i < len(raw)/2; i++ {
			raw[i*2], raw[i*2+1] = raw[i*2+1], raw[i*2]
		}
	}

	swapByte(raw, pt.Swap)

	return interpretValue(raw, pt.Type)
}

// ──────────────────────────────────────────────
//  Modbus request / response (§4.1, §4.2)
// ──────────────────────────────────────────────

func buildRequest(txID uint16, uid uint8, fun uint8, startAddr uint16, quantity uint16) []byte {
	buf := make([]byte, 12)
	binary.BigEndian.PutUint16(buf[0:2], txID)
	binary.BigEndian.PutUint16(buf[2:4], 0)
	binary.BigEndian.PutUint16(buf[4:6], 6)
	buf[6] = uid
	buf[7] = fun
	binary.BigEndian.PutUint16(buf[8:10], startAddr)
	binary.BigEndian.PutUint16(buf[10:12], quantity)
	return buf
}

// readResponse reads and validates a Modbus/TCP response. Returns the PDU
// (function code + data) or an error.
func readResponse(conn net.Conn, txID uint16, uid uint8, t1 time.Duration) ([]byte, error) {
	if err := conn.SetReadDeadline(time.Now().Add(t1)); err != nil {
		return nil, err
	}

	header := make([]byte, 7)
	if _, err := io.ReadFull(conn, header); err != nil {
		return nil, err
	}
	respTxID := binary.BigEndian.Uint16(header[0:2])
	if respTxID != txID {
		return nil, fmt.Errorf("transaction id mismatch: got %d, want %d", respTxID, txID)
	}
	if binary.BigEndian.Uint16(header[2:4]) != 0 {
		return nil, fmt.Errorf("invalid protocol id")
	}
	if header[6] != uid {
		return nil, fmt.Errorf("unit id mismatch: got %d, want %d", header[6], uid)
	}
	length := int(binary.BigEndian.Uint16(header[4:6]))
	if length < 1 || length > 254 {
		return nil, fmt.Errorf("invalid response length %d", length)
	}

	pdu := make([]byte, length-1)
	if _, err := io.ReadFull(conn, pdu); err != nil {
		return nil, err
	}
	return pdu, nil
}

// ──────────────────────────────────────────────
//  Polling
// ──────────────────────────────────────────────

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

func (ist *instanceState) address() string {
	return fmt.Sprintf("%s:%d", ist.cfg.IP, ist.cfg.Port)
}

// sendReceiveBatch sends one request and processes one response. Returns nil on
// success (including exception/malformed responses, which are counted and skipped),
// or an error on transport failure (write error / read timeout / frame mismatch).
func sendReceiveBatch(ist *instanceState, conn net.Conn, b *batch, shmData []byte) error {
	txID := uint16(atomic.AddUint32(&ist.txID, 1))
	req := buildRequest(txID, b.UID, b.Fun, uint16(b.Addr), b.Qty)
	if _, err := conn.Write(req); err != nil {
		return err
	}

	pdu, err := readResponse(conn, txID, b.UID, time.Duration(ist.cfg.T1)*time.Second)
	if err != nil {
		return err
	}
	if len(pdu) < 1 {
		return fmt.Errorf("empty response PDU")
	}

	funCode := pdu[0]
	if funCode == b.Fun|0x80 {
		atomic.AddUint64(&ist.stats.pollErrors, 1)
		return nil
	}
	if funCode != b.Fun {
		atomic.AddUint64(&ist.stats.pollErrors, 1)
		return nil
	}

	if len(pdu) < 2 {
		return fmt.Errorf("truncated response PDU")
	}
	byteCount := int(pdu[1])
	if len(pdu) < 2+byteCount {
		atomic.AddUint64(&ist.stats.pollErrors, 1)
		return nil
	}
	data := pdu[2 : 2+byteCount]

	timestamp := uint64(time.Now().UnixMilli())

	for _, pt := range b.Points {
		if pt.Fun == 1 || pt.Fun == 2 {
			bitOffset := pt.Addr - b.Addr
			byteIndex := bitOffset / 8
			if int(byteIndex) >= len(data) {
				atomic.AddUint64(&ist.stats.itemsDropped, 1)
				continue
			}
			bit := (data[byteIndex] >> (bitOffset % 8)) & 0x01
			writeBlock(shmData, pt.ShmID, pt.Type, timestamp, uint64(bit), 1)
			atomic.AddUint64(&ist.stats.itemsWritten, 1)
			continue
		}

		regOffset := int(pt.Addr-b.Addr) * 2
		if regOffset+int(pt.Span)*2 > len(data) {
			atomic.AddUint64(&ist.stats.itemsDropped, 1)
			continue
		}
		value := decodeRegisterPoint(data, b.Addr, pt, ist.cfg.HtonRegister)
		writeBlock(shmData, pt.ShmID, pt.Type, timestamp, value, valueByteSize(pt.Type))
		atomic.AddUint64(&ist.stats.itemsWritten, 1)
	}
	return nil
}

// pollBatch sends a batch, retrying the request on t1 timeout up to `retries`
// times (retries=0 means unlimited, matching the C libmodbus semantics), then
// gives up. Non-timeout errors (connection broken) abort immediately.
func pollBatch(ist *instanceState, conn net.Conn, b *batch, shmData []byte) error {
	sendCount := 0
	for {
		err := sendReceiveBatch(ist, conn, b, shmData)
		if err == nil {
			return nil
		}

		var netErr net.Error
		if !errors.As(err, &netErr) || !netErr.Timeout() {
			return err
		}

		if ist.cfg.Retries > 0 && sendCount >= ist.cfg.Retries {
			return err
		}
		sendCount++

		select {
		case <-ist.quit:
			return err
		default:
		}
	}
}

var errStopped = errors.New("stopped")

// dialInterruptible dials with a timeout but returns early if quit is closed.
// On early return it drains and closes any connection the background dial may
// still establish, so no connection leaks.
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

func pollRound(ist *instanceState, shmData []byte) {
	select {
	case <-ist.quit:
		return
	default:
	}

	conn := ist.getConn()
	if conn == nil {
		var err error
		conn, err = dialInterruptible(ist.address(), time.Duration(ist.cfg.T0)*time.Second, ist.quit)
		if err != nil {
			return
		}
		ist.setConn(conn)
		atomic.AddUint64(&ist.stats.reconnects, 1)
	}

	for _, b := range ist.batches {
		if err := pollBatch(ist, conn, b, shmData); err != nil {
			atomic.AddUint64(&ist.stats.pollErrors, 1)
			ist.closeConn()
			return
		}
	}
}

func runClient(ist *instanceState, shmData []byte) {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("runClient panic recovered: %v", r)
		}
	}()
	defer ist.wg.Done()
	defer ist.closeConn()

	timer := time.NewTicker(time.Duration(ist.cfg.Timer) * time.Millisecond)
	defer timer.Stop()

	for {
		select {
		case <-ist.quit:
			return
		case <-timer.C:
			pollRound(ist, shmData)
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
	var lastErr string

	for _, cfg := range instances {
		var points []*pointMapping
		for _, pt := range cfg.Points {
			points = append(points, &pointMapping{
				UID:   uint8(pt.UID),
				Fun:   pt.Fun,
				Addr:  pt.Addr,
				ShmID: pt.ShmID,
				Type:  pt.Type,
				Span:  pointSpan(pt.Fun, pt.Type),
				Swap:  pt.Swap,
			})
		}

		ist := &instanceState{
			cfg:     cfg,
			batches: buildBatches(points, cfg.CoilsQuantityMax, cfg.RegistersQuantityMax),
			quit:    make(chan struct{}),
		}

		conn, err := net.DialTimeout("tcp", ist.address(), time.Duration(cfg.T0)*time.Second)
		if err != nil {
			lastErr = fmt.Sprintf("CONNECT_FAILED: connect to %s failed: %v", ist.address(), err)
			break
		}
		ist.conn = conn

		instancesState = append(instancesState, ist)
	}

	if lastErr != "" {
		for _, ist := range instancesState {
			ist.closeConn()
		}
		unix.Munmap(shmData)
		unix.Close(shmFd)
		return newError(lastErr), nil
	}

	for _, ist := range instancesState {
		ist.wg.Add(1)
		go func(ist *instanceState) {
			runClient(ist, shmData)
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
		close(ist.quit)
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
		&mcp.Implementation{Name: "c4_modbus_client", Version: "0.1.0"},
		nil,
	)

	server.AddTool(
		&mcp.Tool{
			Name:        "start",
			Description: "Start Modbus/TCP client polling instances",
			InputSchema: json.RawMessage(`{"type":"object","properties":{"instance_id":{"type":"string"},"config_path":{"type":"string"}},"required":["instance_id","config_path"]}`),
		},
		startHandler,
	)

	mcp.AddTool(server,
		&mcp.Tool{
			Name:        "stop",
			Description: "Stop all Modbus/TCP client instances and release resources",
			InputSchema: json.RawMessage(`{"type":"object","properties":{},"required":[]}`),
		},
		stopHandler,
	)

	if err := server.Run(context.Background(), &mcp.StdioTransport{}); err != nil {
		log.Fatal(err)
	}
}
