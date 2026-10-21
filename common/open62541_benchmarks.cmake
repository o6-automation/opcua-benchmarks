# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
#    Copyright 2026 (c) o6 Automation GmbH (Author: Daniel Opitz)

get_filename_component(BENCH_ROOT "${CMAKE_CURRENT_LIST_DIR}/.." ABSOLUTE)
set(O6_OPEN62541_SOURCE_DIR "${BENCH_ROOT}/deps/open62541"
    CACHE PATH "Path to the vendored open62541 source tree")
get_filename_component(BENCH_NATIVE "${O6_OPEN62541_SOURCE_DIR}" ABSOLUTE)
set(CMAKE_C_STANDARD 99)
set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type")
set(UA_ENABLE_SUBSCRIPTIONS ON CACHE BOOL "" FORCE)
set(UA_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(UA_BUILD_UNIT_TESTS OFF CACHE BOOL "" FORCE)
# Keep only fatal logs so per-request trace/debug calls do not distort measurements.
set(UA_LOGLEVEL 600 CACHE STRING "" FORCE)
if(BENCH_SUITE STREQUAL "throughput")
    set(UA_ENABLE_ENCRYPTION OPENSSL CACHE STRING "" FORCE)
else()
    set(UA_ENABLE_ENCRYPTION OFF CACHE STRING "" FORCE)
endif()
if(BENCH_SUITE STREQUAL "server_capacity")
    set(UA_MULTITHREADING 0 CACHE STRING "" FORCE)
else()
    set(UA_MULTITHREADING 100 CACHE STRING "" FORCE)
endif()
add_subdirectory("${BENCH_NATIVE}" open62541 EXCLUDE_FROM_ALL)

if(BENCH_SUITE STREQUAL "subscription")
    foreach(worker server client)
        add_executable(${worker} "${BENCH_ROOT}/subscription/native/${worker}.c")
        target_compile_options(${worker} PRIVATE -Wall -Wextra -Werror)
        target_link_libraries(${worker} PRIVATE open62541::open62541)
    endforeach()
else()
    include("${BENCH_ROOT}/common/open62541_add_server_executable.cmake")
    if(BENCH_SUITE STREQUAL "throughput")
        add_executable(client "${BENCH_ROOT}/throughput/open62541/client.c")
    elseif(BENCH_SUITE STREQUAL "server_capacity")
        add_executable(client "${BENCH_ROOT}/server_capacity/native/client.c")
        target_include_directories(client PRIVATE "${BENCH_ROOT}/common")
    elseif(BENCH_SUITE STREQUAL "server_limits")
        include("${BENCH_ROOT}/common/open62541_add_hammer_client_executable.cmake")
    else()
        message(FATAL_ERROR "Unknown benchmark suite: ${BENCH_SUITE}")
    endif()
    if(TARGET client)
        target_link_libraries(client PRIVATE open62541::open62541)
    endif()
endif()
foreach(worker server client hammer_client)
    if(TARGET ${worker})
        set_target_properties(${worker} PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}")
        if(UNIX AND NOT APPLE)
            target_compile_definitions(${worker} PRIVATE _GNU_SOURCE)
        endif()
    endif()
endforeach()

# A detached submodule update must reconfigure even if its CMake file is unchanged.
execute_process(COMMAND git rev-parse --absolute-git-dir WORKING_DIRECTORY "${BENCH_NATIVE}"
                OUTPUT_VARIABLE BENCH_GIT_DIR OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY)
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${BENCH_GIT_DIR}/HEAD")
execute_process(COMMAND git rev-parse HEAD WORKING_DIRECTORY "${BENCH_NATIVE}"
                OUTPUT_VARIABLE BENCH_COMMIT OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY)
execute_process(COMMAND git describe --tags --always WORKING_DIRECTORY "${BENCH_NATIVE}"
                OUTPUT_VARIABLE BENCH_VERSION OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY)
file(GENERATE OUTPUT "${CMAKE_BINARY_DIR}/provenance.json" CONTENT
"{\"source_commit\":\"${BENCH_COMMIT}\",\"sdk\":\"${BENCH_VERSION}\",\"compiler\":\"${CMAKE_C_COMPILER_ID} ${CMAKE_C_COMPILER_VERSION}\",\"build_type\":\"${CMAKE_BUILD_TYPE}\",\"log_level\":${UA_LOGLEVEL},\"multithreading\":${UA_MULTITHREADING},\"encryption\":\"${UA_ENABLE_ENCRYPTION}\"}\n")
