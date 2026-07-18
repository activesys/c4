module c4/mcp/c4_shm_manager

go 1.25.0

require golang.org/x/sys v0.47.0 // indirect

require c4/mcp/internal/shm v0.0.0

replace c4/mcp/internal/shm => ../internal/shm

require (
	github.com/google/jsonschema-go v0.4.3 // indirect
	github.com/modelcontextprotocol/go-sdk v1.6.1
	github.com/segmentio/asm v1.1.3 // indirect
	github.com/segmentio/encoding v0.5.4 // indirect
	github.com/yosida95/uritemplate/v3 v3.0.2 // indirect
	golang.org/x/oauth2 v0.35.0 // indirect
)
