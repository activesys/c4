package shm

import (
	"encoding/binary"
	"fmt"
	"os"

	"golang.org/x/sys/unix"
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
	HdrOffRemapVersion   = 6
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
	RemapVersion   uint16
	PointCount     uint32
	MaxPoints      uint32
	GlobalWriteSeq uint64
	Reserved       uint64
}

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
	RemapVersion   int    `json:"remap_version"`
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
	return ShmDir + "/c4_" + instanceID
}

func Create(instanceID string, maxPoints int) (*SharedMemory, error) {
	path := ShmPath(instanceID)
	fd, err := unix.Open(path, unix.O_CREAT|unix.O_EXCL|unix.O_RDWR, 0600)
	if err != nil {
		if os.IsExist(err) {
			return nil, fmt.Errorf("SHM_ALREADY_EXISTS: /dev/shm/c4_%s is already created", instanceID)
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
		binary.BigEndian.PutUint32(data[i*BlockSize+BlkOffMagic:], Magic)
	}

	binary.BigEndian.PutUint16(data[HdrOffVersion:], Version)
	binary.BigEndian.PutUint32(data[HdrOffMaxPoints:], uint32(maxPoints))

	/* WRITE MAGIC LAST as the commit point */
	binary.BigEndian.PutUint32(data[HdrOffMagic:], Magic)

	return &SharedMemory{
		fd:        fd,
		data:      data,
		path:      path,
		maxPoints: maxPoints,
	}, nil
}

func (s *SharedMemory) HeaderInfo() HeaderInfo {
	return HeaderInfo{
		Magic:          binary.BigEndian.Uint32(s.data[HdrOffMagic:]),
		Version:        binary.BigEndian.Uint16(s.data[HdrOffVersion:]),
		RemapVersion:   binary.BigEndian.Uint16(s.data[HdrOffRemapVersion:]),
		PointCount:     binary.BigEndian.Uint32(s.data[HdrOffPointCount:]),
		MaxPoints:      binary.BigEndian.Uint32(s.data[HdrOffMaxPoints:]),
		GlobalWriteSeq: binary.BigEndian.Uint64(s.data[HdrOffGlobalWriteSeq:]),
		Reserved:       binary.BigEndian.Uint64(s.data[HdrOffReserved:]),
	}
}

func (s *SharedMemory) Path() string {
	return s.path
}

func (s *SharedMemory) BlockInfo(shmID int) BlockInfo {
	off := shmID * BlockSize
	return BlockInfo{
		Magic:     binary.BigEndian.Uint32(s.data[off+BlkOffMagic:]),
		State:     s.data[off+BlkOffState],
		Reserved:  binary.BigEndian.Uint16(s.data[off+BlkOffReserved:]),
		Type:      s.data[off+BlkOffType],
		WriteSeq:  binary.BigEndian.Uint64(s.data[off+BlkOffWriteSeq:]),
		Timestamp: binary.BigEndian.Uint64(s.data[off+BlkOffTimestamp:]),
		Value:     binary.BigEndian.Uint64(s.data[off+BlkOffValue:]),
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
	binary.BigEndian.PutUint32(s.data[offset:], val)
}

func (s *SharedMemory) SetHeaderUint16(offset int, val uint16) {
	binary.BigEndian.PutUint16(s.data[offset:], val)
}

func (s *SharedMemory) InitBlock(shmID int) {
	binary.BigEndian.PutUint32(s.data[shmID*BlockSize+BlkOffMagic:], Magic)
}

func (s *SharedMemory) Expand(newMaxPoints int) error {
	oldMax := s.maxPoints
	totalSize := int64((newMaxPoints + 1) * BlockSize)

	if err := unix.Ftruncate(s.fd, totalSize); err != nil {
		return fmt.Errorf("SHM_SYSCALL_FAILED: ftruncate failed - %w", err)
	}

	/* write new max_points to old mmap (persisted to file via MAP_SHARED) */
	binary.BigEndian.PutUint32(s.data[HdrOffMaxPoints:], uint32(newMaxPoints))

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

	/* increment remap_version AFTER new mmap */
	ver := binary.BigEndian.Uint16(s.data[HdrOffRemapVersion:])
	binary.BigEndian.PutUint16(s.data[HdrOffRemapVersion:], ver+1)

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
		Magic:          binary.BigEndian.Uint32(data[HdrOffMagic:]),
		Version:        binary.BigEndian.Uint16(data[HdrOffVersion:]),
		RemapVersion:   binary.BigEndian.Uint16(data[HdrOffRemapVersion:]),
		PointCount:     binary.BigEndian.Uint32(data[HdrOffPointCount:]),
		MaxPoints:      binary.BigEndian.Uint32(data[HdrOffMaxPoints:]),
		GlobalWriteSeq: binary.BigEndian.Uint64(data[HdrOffGlobalWriteSeq:]),
		Reserved:       binary.BigEndian.Uint64(data[HdrOffReserved:]),
	}, nil
}
