package shm

import (
	"encoding/binary"
	"errors"
	"fmt"
	"os"

	"golang.org/x/sys/unix"
)

// Sentinel errors for the attach/create decision in c4_shm_manager tool
// calls (c4_shm_manager.md §1.3 / §3.1):
//   - ErrMissing: the shm object does not exist (create branch is reachable)
//   - ErrExists:  the shm object came into existence between the attach probe
//     and O_CREAT|O_EXCL (concurrency race guard; retry attach)
var (
	ErrMissing = errors.New("shm object does not exist")
	ErrExists  = errors.New("shm object already exists")
)

const (
	BlockSize        = 32
	Magic     uint32 = 0xC4DA7A00
	Version   uint16 = 1

	DefaultMaxPoints = 100000
	ShmDir           = "/dev/shm"
)

const (
	HdrOffMagic          = 0
	HdrOffVersion        = 4
	HdrOffReserved2      = 6
	HdrOffPointCount     = 8
	HdrOffMaxPoints      = 12
	HdrOffGlobalWriteSeq = 16
	HdrOffReserved       = 24
)

const (
	BlkOffMagic     = 0
	BlkOffState     = 4
	BlkOffReserved  = 5
	BlkOffType      = 7
	BlkOffWriteSeq  = 8
	BlkOffTimestamp = 16
	BlkOffValue     = 24
)

type HeaderInfo struct {
	Magic          uint32
	Version        uint16
	Reserved2      uint16
	PointCount     uint32
	MaxPoints      uint32
	GlobalWriteSeq uint64
	Reserved       uint64
}

// BlockInfo is a convenience container. Fields are populated from raw bytes
// using explicit BlkOff* offsets — do NOT use this struct for direct memory
// mapping via unsafe.Pointer; Go's padding does not match the SHM layout.
type BlockInfo struct {
	Magic     uint32
	State     uint8
	Reserved  uint16
	Type      uint8
	WriteSeq  uint64
	Timestamp uint64
	Value     uint64
}

type StatusInfo struct {
	Magic          string `json:"magic"`
	Version        int    `json:"version"`
	Reserved2      uint16 `json:"reserved"`
	PointCount     int    `json:"point_count"`
	MaxPoints      int    `json:"max_points"`
	FreeBlocks     int    `json:"free_blocks"`
	GlobalWriteSeq uint64 `json:"global_write_seq"`
}

type SharedMemory struct {
	fd        int
	data      []byte
	path      string
	maxPoints int
}

func ShmPath(instanceID string) string {
	return ShmDir + "/" + instanceID
}

// Open attaches to an existing shared memory object (O_RDWR, no O_CREAT).
// It validates the header magic and version, reconciles header max_points
// against the file size (crash between ftruncate and the max_points update —
// c4_shm_manager.md §3.2 部分失败恢复), and maps the full size.
func Open(path string) (*SharedMemory, error) {
	fd, err := unix.Open(path, unix.O_RDWR, 0)
	if err != nil {
		if errors.Is(err, unix.ENOENT) {
			return nil, fmt.Errorf("%w: shm_open failed for %s: %v", ErrMissing, path, err)
		}
		return nil, fmt.Errorf("SHM_OPEN_FAILED: shm_open failed for %s: %w", path, err)
	}

	hdrData, err := unix.Mmap(fd, 0, BlockSize, unix.PROT_READ, unix.MAP_SHARED)
	if err != nil {
		unix.Close(fd)
		return nil, fmt.Errorf("SHM_OPEN_FAILED: mmap header failed: %w", err)
	}
	magic := binary.NativeEndian.Uint32(hdrData[0:])
	if magic != Magic {
		unix.Munmap(hdrData)
		unix.Close(fd)
		return nil, fmt.Errorf("SHM_CORRUPTED: header magic is invalid (got 0x%08X, expected 0x%08X)", magic, Magic)
	}
	if version := binary.NativeEndian.Uint16(hdrData[HdrOffVersion:]); version != Version {
		unix.Munmap(hdrData)
		unix.Close(fd)
		return nil, fmt.Errorf("SHM_CORRUPTED: header version is unsupported (got %d, expected %d)", version, Version)
	}
	maxPoints := binary.NativeEndian.Uint32(hdrData[HdrOffMaxPoints:])
	unix.Munmap(hdrData)

	/* reconcile header max_points with the actual file size: a crash between
	   ftruncate and the max_points update leaves file_size > declared size —
	   the file size is authoritative */
	var st unix.Stat_t
	if err := unix.Fstat(fd, &st); err != nil {
		unix.Close(fd)
		return nil, fmt.Errorf("SHM_OPEN_FAILED: fstat failed: %w", err)
	}
	if fileBlocks := uint32(st.Size) / BlockSize; fileBlocks >= 1 && fileBlocks-1 != maxPoints {
		rwHdr, err := unix.Mmap(fd, 0, BlockSize, unix.PROT_READ|unix.PROT_WRITE, unix.MAP_SHARED)
		if err != nil {
			unix.Close(fd)
			return nil, fmt.Errorf("SHM_OPEN_FAILED: mmap header failed: %w", err)
		}
		maxPoints = fileBlocks - 1
		binary.NativeEndian.PutUint32(rwHdr[HdrOffMaxPoints:], maxPoints)
		unix.Munmap(rwHdr)
	}

	totalSize := int64(int(maxPoints)+1) * BlockSize
	data, err := unix.Mmap(fd, 0, int(totalSize), unix.PROT_READ|unix.PROT_WRITE, unix.MAP_SHARED)
	if err != nil {
		unix.Close(fd)
		return nil, fmt.Errorf("SHM_OPEN_FAILED: mmap failed: %w", err)
	}

	return &SharedMemory{
		fd:        fd,
		data:      data,
		path:      path,
		maxPoints: int(maxPoints),
	}, nil
}

func Create(instanceID string, maxPoints int) (*SharedMemory, error) {
	path := ShmPath(instanceID)
	fd, err := unix.Open(path, unix.O_CREAT|unix.O_EXCL|unix.O_RDWR, 0600)
	if err != nil {
		if os.IsExist(err) {
			return nil, fmt.Errorf("%w: %s", ErrExists, instanceID)
		}
		return nil, fmt.Errorf("SHM_SYSCALL_FAILED: shm_open failed - %w", err)
	}

	totalSize := int64((maxPoints + 1) * BlockSize)
	if err := unix.Ftruncate(fd, totalSize); err != nil {
		unix.Close(fd)
		unix.Unlink(path)
		return nil, fmt.Errorf("SHM_SYSCALL_FAILED: ftruncate failed - %w", err)
	}

	data, err := unix.Mmap(fd, 0, int(totalSize), unix.PROT_READ|unix.PROT_WRITE, unix.MAP_SHARED)
	if err != nil {
		unix.Close(fd)
		unix.Unlink(path)
		return nil, fmt.Errorf("SHM_SYSCALL_FAILED: mmap failed - %w", err)
	}

	for i := 1; i <= maxPoints; i++ {
		binary.NativeEndian.PutUint32(data[i*BlockSize+BlkOffMagic:], Magic)
	}

	binary.NativeEndian.PutUint16(data[HdrOffVersion:], Version)
	binary.NativeEndian.PutUint32(data[HdrOffMaxPoints:], uint32(maxPoints))

	/* WRITE MAGIC LAST as the commit point */
	binary.NativeEndian.PutUint32(data[HdrOffMagic:], Magic)

	return &SharedMemory{
		fd:        fd,
		data:      data,
		path:      path,
		maxPoints: maxPoints,
	}, nil
}

func (s *SharedMemory) HeaderInfo() HeaderInfo {
	return HeaderInfo{
		Magic:          binary.NativeEndian.Uint32(s.data[HdrOffMagic:]),
		Version:        binary.NativeEndian.Uint16(s.data[HdrOffVersion:]),
		Reserved2:      binary.NativeEndian.Uint16(s.data[HdrOffReserved2:]),
		PointCount:     binary.NativeEndian.Uint32(s.data[HdrOffPointCount:]),
		MaxPoints:      binary.NativeEndian.Uint32(s.data[HdrOffMaxPoints:]),
		GlobalWriteSeq: binary.NativeEndian.Uint64(s.data[HdrOffGlobalWriteSeq:]),
		Reserved:       binary.NativeEndian.Uint64(s.data[HdrOffReserved:]),
	}
}

func (s *SharedMemory) Path() string {
	return s.path
}

func (s *SharedMemory) BlockInfo(shmID int) BlockInfo {
	off := shmID * BlockSize
	return BlockInfo{
		Magic:     binary.NativeEndian.Uint32(s.data[off+BlkOffMagic:]),
		State:     s.data[off+BlkOffState],
		Reserved:  binary.NativeEndian.Uint16(s.data[off+BlkOffReserved:]),
		Type:      s.data[off+BlkOffType],
		WriteSeq:  binary.NativeEndian.Uint64(s.data[off+BlkOffWriteSeq:]),
		Timestamp: binary.NativeEndian.Uint64(s.data[off+BlkOffTimestamp:]),
		Value:     binary.NativeEndian.Uint64(s.data[off+BlkOffValue:]),
	}
}

func (s *SharedMemory) Close() error {
	if err := unix.Munmap(s.data); err != nil {
		return fmt.Errorf("SHM_SYSCALL_FAILED: munmap failed - %w", err)
	}
	if err := unix.Close(s.fd); err != nil {
		return fmt.Errorf("SHM_SYSCALL_FAILED: close failed - %w", err)
	}
	return nil
}

func (s *SharedMemory) Unlink() error {
	if err := s.Close(); err != nil {
		return err
	}
	if err := unix.Unlink(s.path); err != nil {
		return fmt.Errorf("SHM_SYSCALL_FAILED: shm_unlink failed - %w", err)
	}
	return nil
}

func (s *SharedMemory) SetHeaderUint32(offset int, val uint32) {
	binary.NativeEndian.PutUint32(s.data[offset:], val)
}

func (s *SharedMemory) SetHeaderUint16(offset int, val uint16) {
	binary.NativeEndian.PutUint16(s.data[offset:], val)
}

func (s *SharedMemory) InitBlock(shmID int) {
	binary.NativeEndian.PutUint32(s.data[shmID*BlockSize+BlkOffMagic:], Magic)
}

func (s *SharedMemory) SetBlockState(shmID int, state uint8) {
	s.data[shmID*BlockSize+BlkOffState] = state
}

func (s *SharedMemory) Expand(newMaxPoints int) error {
	oldMax := s.maxPoints
	totalSize := int64((newMaxPoints + 1) * BlockSize)

	if err := unix.Ftruncate(s.fd, totalSize); err != nil {
		return fmt.Errorf("SHM_SYSCALL_FAILED: ftruncate failed - %w", err)
	}

	/* write new max_points to old mmap (persisted to file via MAP_SHARED) */
	binary.NativeEndian.PutUint32(s.data[HdrOffMaxPoints:], uint32(newMaxPoints))

	/* munmap old mapping */
	if err := unix.Munmap(s.data); err != nil {
		return fmt.Errorf("SHM_SYSCALL_FAILED: munmap failed - %w", err)
	}

	/* mmap new size */
	data, err := unix.Mmap(s.fd, 0, int(totalSize), unix.PROT_READ|unix.PROT_WRITE, unix.MAP_SHARED)
	if err != nil {
		return fmt.Errorf("SHM_SYSCALL_FAILED: mmap failed - %w", err)
	}
	s.data = data
	s.maxPoints = newMaxPoints

	/* init new blocks (oldMax+1 .. newMaxPoints) */
	for i := oldMax + 1; i <= newMaxPoints; i++ {
		s.InitBlock(i)
	}

	return nil
}

func (s *SharedMemory) FindFreeBlocks(maxPoints int) []uint32 {
	var free []uint32
	for i := 1; i <= maxPoints; i++ {
		if s.data[i*BlockSize+BlkOffState] == 0 {
			free = append(free, uint32(i))
		}
	}
	return free
}

func ReadHeaderFromPath(path string) (HeaderInfo, error) {
	fd, err := unix.Open(path, unix.O_RDONLY, 0)
	if err != nil {
		return HeaderInfo{}, fmt.Errorf("SHM_SYSCALL_FAILED: open failed - %w", err)
	}
	defer unix.Close(fd)

	data, err := unix.Mmap(fd, 0, BlockSize, unix.PROT_READ, unix.MAP_SHARED)
	if err != nil {
		return HeaderInfo{}, fmt.Errorf("SHM_SYSCALL_FAILED: mmap failed - %w", err)
	}
	defer unix.Munmap(data)

	return HeaderInfo{
		Magic:          binary.NativeEndian.Uint32(data[HdrOffMagic:]),
		Version:        binary.NativeEndian.Uint16(data[HdrOffVersion:]),
		Reserved2:      binary.NativeEndian.Uint16(data[HdrOffReserved2:]),
		PointCount:     binary.NativeEndian.Uint32(data[HdrOffPointCount:]),
		MaxPoints:      binary.NativeEndian.Uint32(data[HdrOffMaxPoints:]),
		GlobalWriteSeq: binary.NativeEndian.Uint64(data[HdrOffGlobalWriteSeq:]),
		Reserved:       binary.NativeEndian.Uint64(data[HdrOffReserved:]),
	}, nil
}
