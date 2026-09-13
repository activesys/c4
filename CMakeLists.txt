# C4 — Agent + MCP 服务集群（Go / TypeScript / Python）
#
# 构建（见 AGENTS.md / .claude/guides）：cd c4/build && cmake -S .. -B . && make
# 目标名 = mcp/ 下的服务目录名；`make` 默认构建全部六个 Go MCP 服务。
cmake_minimum_required(VERSION 3.10)
project(c4 NONE)

find_program(GO_EXECUTABLE go REQUIRED)

set(C4_MCP_SERVICES
    c4_shm_manager
    c4_modbus_client
    c4_iec104_client
    c4_asfp2_client
    c4_asfp2_server
    c4_influxdb_client
)

foreach(svc IN LISTS C4_MCP_SERVICES)
    add_custom_target(${svc}
        COMMAND ${GO_EXECUTABLE} build -o ${svc} .
        WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/mcp/${svc}
        COMMENT "Building ${svc} (go)"
        VERBATIM
    )
endforeach()

add_custom_target(mcp ALL DEPENDS ${C4_MCP_SERVICES})
