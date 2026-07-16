package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"strings"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"c4/mcp/c4_shm_manager/internal/shm"
)

var state = &serverState{}

type serverState struct {
	currentInstanceID string
	sm                *shm.SharedMemory
}

type CreateShmInput struct {
	InstanceID string `json:"instance_id" jsonschema:"required"`
}

func createShmHandler(ctx context.Context, req *mcp.CallToolRequest, input CreateShmInput) (
	*mcp.CallToolResult, any, error,
) {
	rootRes, err := req.Session.ListRoots(ctx, nil)
	if err != nil {
		return newError("CONFIG_PATH_MISSING: roots/list protocol call failed, Agent may not be responding"), nil, nil
	}

	var sm *shm.SharedMemory

	if shouldUseDefault(rootRes) {
		sm, err = shm.Create(input.InstanceID, shm.DefaultMaxPoints)
	} else {
		configPath := rootRes.Roots[0].URI
		if len(configPath) > 7 && configPath[:7] == "file://" {
			configPath = configPath[7:]
		}
		sm, err = createFromConfig(configPath, input.InstanceID)
	}
	if err != nil {
		return newError(err.Error()), nil, nil
	}

	if state.sm != nil {
		state.sm.Close()
	}
	state.sm = sm
	state.currentInstanceID = input.InstanceID

	return newResult("success"), nil, nil
}

func shouldUseDefault(rootRes *mcp.ListRootsResult) bool {
	if rootRes == nil || len(rootRes.Roots) == 0 {
		return true
	}
	uri := rootRes.Roots[0].URI
	configPath := uri
	if len(uri) > 7 && uri[:7] == "file://" {
		configPath = uri[7:]
	}
	if _, err := os.Stat(configPath); os.IsNotExist(err) {
		return true
	}
	data, err := os.ReadFile(configPath)
	if err != nil {
		return true
	}
	return isWhitespaceOnly(data) || isEmptyJSON(data)
}

func isWhitespaceOnly(data []byte) bool {
	for _, b := range data {
		switch b {
		case ' ', '\t', '\n', '\r':
			continue
		default:
			return false
		}
	}
	return true
}

func isEmptyJSON(data []byte) bool {
	s := strings.TrimSpace(string(data))
	return s == "{}" || s == "null"
}

func createFromConfig(configPath string, instanceID string) (*shm.SharedMemory, error) {
	data, err := os.ReadFile(configPath)
	if err != nil {
		return nil, fmt.Errorf("CONFIG_MISSING_SECTION: cannot read config file: %v", err)
	}

	var config map[string]any
	if err := json.Unmarshal(data, &config); err != nil {
		return nil, fmt.Errorf("CONFIG_PARSE_ERROR: failed to parse config JSON: %v", err)
	}

	shmCfg, ok := config["c4_shm_manager"].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("CONFIG_MISSING_SECTION: 'c4_shm_manager' key not found in config")
	}

	writers, writersOk := toStringSlice(shmCfg["writer"])
	readers, readersOk := toStringSlice(shmCfg["reader"])
	if !writersOk || len(writers) == 0 {
		return nil, fmt.Errorf("CONFIG_MISSING_SECTION: 'c4_shm_manager.writer' not found or empty in config")
	}
	if !readersOk || len(readers) == 0 {
		return nil, fmt.Errorf("CONFIG_MISSING_SECTION: 'c4_shm_manager.reader' not found or empty in config")
	}

	totalPoints := 0
	for _, wType := range writers {
		section, ok := config[wType]
		if !ok {
			continue
		}
		instances, ok := section.([]any)
		if !ok {
			continue
		}
		for _, inst := range instances {
			instMap, ok := inst.(map[string]any)
			if !ok {
				continue
			}
			pts, _ := instMap["points"].([]any)
			totalPoints += len(pts)
		}
	}
	if totalPoints == 0 {
		return nil, fmt.Errorf("CONFIG_MISSING_SECTION: writer points total is 0 after counting")
	}

	maxPoints := totalPoints * 2
	sm, err := shm.Create(instanceID, maxPoints)
	if err != nil {
		return nil, err
	}

	keyMap := make(map[string]int)
	nextID := 1
	for _, wType := range writers {
		section, ok := config[wType]
		if !ok {
			continue
		}
		instances, _ := section.([]any)
		for _, inst := range instances {
			instMap := inst.(map[string]any)
			serviceID, _ := instMap["id"].(string)
			pts, _ := instMap["points"].([]any)
			for _, pt := range pts {
				ptMap := pt.(map[string]any)
				pointID, _ := ptMap["id"].(string)
				key := serviceID + "." + pointID
				if _, exists := keyMap[key]; exists {
					rollback(sm)
					return nil, fmt.Errorf("DUPLICATE_KEY: key '%s' already assigned", key)
				}
				keyMap[key] = nextID
				ptMap["shm_id"] = float64(nextID)
				nextID++
			}
		}
	}

	for _, rType := range readers {
		section, ok := config[rType]
		if !ok {
			continue
		}
		instances, _ := section.([]any)
		for _, inst := range instances {
			instMap := inst.(map[string]any)
			pts, _ := instMap["points"].([]any)
			for _, pt := range pts {
				ptMap := pt.(map[string]any)
				key, _ := ptMap["key"].(string)
				pid, exists := keyMap[key]
				if !exists {
					rollback(sm)
					return nil, fmt.Errorf("UNKNOWN_READER_KEY: reader key '%s' not found in any writer", key)
				}
				ptMap["shm_id"] = float64(pid)
			}
		}
	}

	sm.SetHeaderUint32(8, uint32(totalPoints))

	out, err := json.MarshalIndent(config, "", "  ")
	if err != nil {
		rollback(sm)
		return nil, fmt.Errorf("CONFIG_WRITE_FAILED: marshal failed: %v", err)
	}

	tmpPath := configPath + ".tmp"
	if err := os.WriteFile(tmpPath, out, 0644); err != nil {
		rollback(sm)
		return nil, fmt.Errorf("CONFIG_WRITE_FAILED: write failed: %v", err)
	}
	if err := os.Rename(tmpPath, configPath); err != nil {
		rollback(sm)
		os.Remove(tmpPath)
		return nil, fmt.Errorf("CONFIG_WRITE_FAILED: rename failed: %v", err)
	}

	return sm, nil
}

func rollback(sm *shm.SharedMemory) {
	sm.Unlink()
}

func toStringSlice(v any) ([]string, bool) {
	arr, ok := v.([]any)
	if !ok {
		return nil, false
	}
	out := make([]string, len(arr))
	for i, item := range arr {
		s, ok := item.(string)
		if !ok {
			return nil, false
		}
		out[i] = s
	}
	return out, true
}

func queryStatusHandler(ctx context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	if state.sm == nil {
		return newError("SHM_NOT_CREATED: shared memory not initialized, call create_shm first"), nil
	}

	h := state.sm.HeaderInfo()
	if h.Magic != shm.Magic {
		return newError("SHM_CORRUPTED: header magic is invalid"), nil
	}

	status := shm.StatusInfo{
		Magic:          "valid",
		Version:        int(h.Version),
		RemapVersion:   int(h.RemapVersion),
		PointCount:     int(h.PointCount),
		MaxPoints:      int(h.MaxPoints),
		FreeBlocks:     int(h.MaxPoints) - int(h.PointCount),
		GlobalWriteSeq: h.GlobalWriteSeq,
	}

	data, err := json.Marshal(status)
	if err != nil {
		return newError(fmt.Sprintf("SHM_SYSCALL_FAILED: marshal failed - %v", err)), nil
	}
	return newResult(string(data)), nil
}

func adjustShmHandler(ctx context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	if state.sm == nil {
		return newError("SHM_NOT_CREATED: shared memory not initialized, call create_shm first"), nil
	}

	if _, err := os.Stat(state.sm.Path()); os.IsNotExist(err) {
		state.sm = nil
		state.currentInstanceID = ""
		return newError("SHM_NOT_CREATED: shared memory not initialized, call create_shm first"), nil
	}

	rootRes, err := req.Session.ListRoots(ctx, nil)
	if err != nil || rootRes == nil || len(rootRes.Roots) == 0 {
		return newError("CONFIG_PATH_MISSING: roots/list protocol call failed, Agent may not be responding"), nil
	}

	configPath := rootRes.Roots[0].URI
	if len(configPath) > 7 && configPath[:7] == "file://" {
		configPath = configPath[7:]
	}

	data, err := os.ReadFile(configPath)
	if err != nil {
		return newError("CONFIG_MISSING_SECTION: cannot read config file: " + err.Error()), nil
	}

	var config map[string]any
	if err := json.Unmarshal(data, &config); err != nil {
		return newError(fmt.Sprintf("CONFIG_PARSE_ERROR: failed to parse config JSON: %v", err)), nil
	}

	shmCfg, ok := config["c4_shm_manager"].(map[string]any)
	if !ok {
		return newError("CONFIG_MISSING_SECTION: 'c4_shm_manager' key not found in config"), nil
	}

	writers, writersOk := toStringSlice(shmCfg["writer"])
	readers, readersOk := toStringSlice(shmCfg["reader"])
	if !writersOk || len(writers) == 0 || !readersOk || len(readers) == 0 {
		return newError("CONFIG_MISSING_SECTION: 'c4_shm_manager.writer' or 'c4_shm_manager.reader' not found or empty in config"), nil
	}

	requiredPoints := 0
	for _, wType := range writers {
		section, ok := config[wType]
		if !ok {
			continue
		}
		instances, ok := section.([]any)
		if !ok {
			continue
		}
		for _, inst := range instances {
			instMap, ok := inst.(map[string]any)
			if !ok {
				continue
			}
			pts, _ := instMap["points"].([]any)
			requiredPoints += len(pts)
		}
	}

	if requiredPoints == 0 {
		return newError("CONFIG_MISSING_SECTION: 'c4_shm_manager.writer' or 'c4_shm_manager.reader' not found or empty in config"), nil
	}

	h := state.sm.HeaderInfo()
	currentMaxPoints := int(h.MaxPoints)

	keyMap := make(map[string]int)
	assignedSet := make(map[int]bool)
	for _, wType := range writers {
		section, ok := config[wType]
		if !ok {
			continue
		}
		instances, _ := section.([]any)
		for _, inst := range instances {
			instMap := inst.(map[string]any)
			serviceID, _ := instMap["id"].(string)
			pts, _ := instMap["points"].([]any)
			for _, pt := range pts {
				ptMap := pt.(map[string]any)
				pointID, _ := ptMap["id"].(string)
				key := serviceID + "." + pointID

				if existingID, exists := keyMap[key]; exists {
					return newError(fmt.Sprintf("DUPLICATE_KEY: key '%s' already assigned to shm_id=%d", key, existingID)), nil
				}

				shmID := 0
				if sid, ok := ptMap["shm_id"].(float64); ok {
					shmID = int(sid)
				}

				keyMap[key] = shmID
				if shmID > 0 {
					assignedSet[shmID] = true
				}
			}
		}
	}

	/* reclaim orphan blocks: scan state=1 blocks, reclaim any whose shm_id is not in assignedSet */
	for shmID := 1; shmID <= currentMaxPoints; shmID++ {
		bi := state.sm.BlockInfo(shmID)
		if bi.State == 1 && !assignedSet[shmID] {
			state.sm.SetBlockState(shmID, 0)
		}
	}

	needsExpand := requiredPoints > currentMaxPoints

	/* assign shm_id to new points (config maps only, no shm side effects yet) */
	nextID := 1
	for _, wType := range writers {
		section := config[wType]
		instances := section.([]any)
		for _, inst := range instances {
			instMap := inst.(map[string]any)
			pts := instMap["points"].([]any)
			for _, pt := range pts {
				ptMap := pt.(map[string]any)
				shmID := 0
				if sid, ok := ptMap["shm_id"].(float64); ok {
					shmID = int(sid)
				}
				if shmID == 0 {
					for assignedSet[nextID] {
						nextID++
					}
					ptMap["shm_id"] = float64(nextID)

					serviceID, _ := instMap["id"].(string)
					pointID, _ := ptMap["id"].(string)
					key := serviceID + "." + pointID
					keyMap[key] = nextID

					assignedSet[nextID] = true
					nextID++
				}
			}
		}
	}

	/* resolve reader keys BEFORE touching shm */
	for _, rType := range readers {
		section, ok := config[rType]
		if !ok {
			continue
		}
		instances, _ := section.([]any)
		for _, inst := range instances {
			instMap := inst.(map[string]any)
			pts, _ := instMap["points"].([]any)
			for _, pt := range pts {
				ptMap := pt.(map[string]any)
				key, _ := ptMap["key"].(string)
				pid, exists := keyMap[key]
				if !exists {
					return newError(fmt.Sprintf("UNKNOWN_READER_KEY: reader key '%s' not found in any writer", key)), nil
				}
				ptMap["shm_id"] = float64(pid)
			}
		}
	}

	/* now apply shm changes — reader validation passed */
	if needsExpand {
		newMaxPoints := requiredPoints * 2
		if err := state.sm.Expand(newMaxPoints); err != nil {
			return newError(err.Error()), nil
		}
	}
	state.sm.SetHeaderUint32(8, uint32(requiredPoints))

	out, err := json.MarshalIndent(config, "", "  ")
	if err != nil {
		return newError(fmt.Sprintf("CONFIG_WRITE_FAILED: marshal failed: %v", err)), nil
	}

	tmpPath := configPath + ".tmp"
	if err := os.WriteFile(tmpPath, out, 0644); err != nil {
		return newError(fmt.Sprintf("CONFIG_WRITE_FAILED: write failed: %v", err)), nil
	}
	if err := os.Rename(tmpPath, configPath); err != nil {
		os.Remove(tmpPath)
		return newError(fmt.Sprintf("CONFIG_WRITE_FAILED: rename failed: %v", err)), nil
	}

	return newResult("success"), nil
}

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

func main() {
	server := mcp.NewServer(
		&mcp.Implementation{Name: "c4_shm_manager", Version: "0.1.0"},
		nil,
	)

	mcp.AddTool(server,
		&mcp.Tool{Name: "create_shm", Description: "Create POSIX shared memory with config-based or default sizing"},
		createShmHandler,
	)

	server.AddTool(
		&mcp.Tool{
			Name:        "query_status",
			Description: "Query shared memory status",
			InputSchema: json.RawMessage(`{"type":"object","properties":{},"required":[]}`),
		},
		queryStatusHandler,
	)

	server.AddTool(
		&mcp.Tool{
			Name:        "adjust_shm",
			Description: "Adjust shared memory capacity and point allocation based on config file",
			InputSchema: json.RawMessage(`{"type":"object","properties":{},"required":[]}`),
		},
		adjustShmHandler,
	)

	if err := server.Run(context.Background(), &mcp.StdioTransport{}); err != nil {
		log.Fatal(err)
	}
}
