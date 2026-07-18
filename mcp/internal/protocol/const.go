// Package protocol defines ASFP2 protocol constants shared by client and server.
package protocol

const MaxAddr = 0xFFFFFF

const (
	FlagV200 = "ASFPV200"
	FlagV210 = "ASFPV210"
	FlagV211 = "ASFPV211"
)

const (
	TypeBoolean        uint8 = 0
	TypeInt8           uint8 = 1
	TypeUint8          uint8 = 2
	TypeInt16          uint8 = 3
	TypeUint16         uint8 = 4
	TypeInt32          uint8 = 5
	TypeUint32         uint8 = 6
	TypeInt64          uint8 = 7
	TypeUint64         uint8 = 8
	TypeFloat16        uint8 = 9
	TypeFloat32        uint8 = 10
	TypeFloat64        uint8 = 11
	TypeString         uint8 = 12
	TypeBlob           uint8 = 13
	TypeBitstring      uint8 = 14
	TypeBit            uint8 = 15
	TypeLargeDataBlock uint8 = 16
)

const (
	AttrKeySequence   = 0x00000001
	AttrSameDataType  = 0x00000002
	AttrSameTimestamp = 0x00000004
)

var VariableTypes = map[uint8]bool{
	TypeString:         true,
	TypeBlob:           true,
	TypeBitstring:      true,
	TypeLargeDataBlock: true,
}

func TypeByteSize(t uint8) int {
	switch t {
	case TypeBoolean, TypeBit:
		return 1
	case TypeInt8, TypeUint8:
		return 1
	case TypeInt16, TypeUint16, TypeFloat16:
		return 2
	case TypeInt32, TypeUint32, TypeFloat32:
		return 4
	case TypeInt64, TypeUint64, TypeFloat64:
		return 8
	default:
		return 0
	}
}
