module c4/mcp/c4_asfp2_server

go 1.25.0

require (
	github.com/modelcontextprotocol/go-sdk v1.6.1
	golang.org/x/sys v0.47.0
)

require c4/mcp/internal/protocol v0.0.0

replace c4/mcp/internal/protocol => ../internal/protocol

require (
	github.com/google/jsonschema-go v0.4.3 // indirect
	github.com/segmentio/asm v1.1.3 // indirect
	github.com/segmentio/encoding v0.5.4 // indirect
	github.com/yosida95/uritemplate/v3 v3.0.2 // indirect
	golang.org/x/oauth2 v0.35.0 // indirect
)

require c4/mcp/internal/shm v0.0.0

replace c4/mcp/internal/shm => ../internal/shm
