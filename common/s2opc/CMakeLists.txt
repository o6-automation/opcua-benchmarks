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
#    Copyright 2026 (c) Sterfive (Author: Etienne Rossignon)

cmake_minimum_required(VERSION 3.21)
project(o6_s2opc_server C)

# The S2OPC toolkit, mbedtls and expat installed under deps/s2opc by
# python -m bench.build --s2opc.
find_package(s2opc REQUIRED CONFIG)

add_executable(server server.c)
target_include_directories(server PRIVATE "${O6_REPOSITORY_ROOT}")
# The expat NodeSet loader is compiled into s2opc_clientserver when expat is found.
target_link_libraries(server PRIVATE s2opc_clientserver)
