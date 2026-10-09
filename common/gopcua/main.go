// SPDX-License-Identifier: AGPL-3.0-or-later
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU Affero General Public License as published
// by the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
// GNU Affero General Public License for more details.
//
// You should have received a copy of the GNU Affero General Public License
// along with this program. If not, see <https://www.gnu.org/licenses/>.
//
//    Copyright 2026 (c) Sterfive (Author: Etienne Rossignon)

// The gopcua benchmark server: the shared address space of
// common/servers/open62541_server.c, served by github.com/gopcua/opcua/server.
package main

import (
	"context"
	"crypto/rsa"
	"crypto/x509"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"runtime"
	"runtime/debug"
	"strconv"
	"strings"
	"syscall"

	"github.com/gopcua/opcua/id"
	"github.com/gopcua/opcua/server"
	"github.com/gopcua/opcua/server/attrs"
	"github.com/gopcua/opcua/uacp"
	"github.com/gopcua/opcua/ua"
)

const (
	firstNodeID      = 1001
	nodeCount        = 100
	arrayFirstNodeID = 2001
	maxArraySizes    = 16
	serverReady      = "benchmark server ready"
	applicationURI   = "urn:o6:benchmark:server"
	// Room for a 4k frame (8,294,400 Int32 values, 33 MB) plus framing.
	maxMessageSize = 64 << 20
)

func parseSizes(text string) ([]int, error) {
	var sizes []int
	for _, token := range strings.Split(text, ",") {
		token = strings.TrimSpace(token)
		if token == "" {
			continue
		}
		value, err := strconv.Atoi(token)
		if err != nil || value <= 0 {
			return nil, fmt.Errorf("invalid array size %q", token)
		}
		duplicate := false
		for _, existing := range sizes {
			duplicate = duplicate || existing == value
		}
		if !duplicate {
			sizes = append(sizes, value)
		}
	}
	if len(sizes) > maxArraySizes {
		return nil, errors.New("too many array sizes")
	}
	return sizes, nil
}

func loadKey(path string) (*rsa.PrivateKey, error) {
	bytes, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	// The shared generator writes DER; accept either RSA container.
	if key, err := x509.ParsePKCS1PrivateKey(bytes); err == nil {
		return key, nil
	}
	parsed, err := x509.ParsePKCS8PrivateKey(bytes)
	if err != nil {
		return nil, errors.New("private key is neither PKCS#1 nor PKCS#8 DER")
	}
	key, ok := parsed.(*rsa.PrivateKey)
	if !ok {
		return nil, errors.New("private key is not RSA")
	}
	return key, nil
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if value != "" {
			return value
		}
	}
	return ""
}

func variable(ns uint16, nodeID uint32, name string, initial any, dimensions []uint32) *server.Node {
	access := byte(ua.AccessLevelTypeCurrentRead | ua.AccessLevelTypeCurrentWrite)
	valueRank := int32(-1)
	if dimensions != nil {
		valueRank = 1
	}
	value := server.DataValueFromValue(initial)
	node := server.NewNode(
		ua.NewNumericNodeID(ns, nodeID),
		map[ua.AttributeID]*ua.DataValue{
			ua.AttributeIDNodeClass:       server.DataValueFromValue(uint32(ua.NodeClassVariable)),
			ua.AttributeIDBrowseName:      server.DataValueFromValue(attrs.BrowseName(name)),
			ua.AttributeIDDisplayName:     server.DataValueFromValue(attrs.DisplayName(name, "en-US")),
			ua.AttributeIDDataType:        server.DataValueFromValue(ua.NewNumericNodeID(0, id.Int32)),
			ua.AttributeIDValueRank:       server.DataValueFromValue(valueRank),
			ua.AttributeIDAccessLevel:     server.DataValueFromValue(access),
			ua.AttributeIDUserAccessLevel: server.DataValueFromValue(access),
		},
		nil,
		func() *ua.DataValue { return value },
	)
	if dimensions != nil {
		node.SetAttribute(ua.AttributeIDArrayDimensions, server.DataValueFromValue(dimensions))
	}
	return node
}

func run() error {
	port := flag.Int("port", 4840, "TCP port")
	security := flag.String("security", "None", "None or Basic256Sha256")
	certificate := flag.String("certificate", "", "DER server certificate")
	privateKey := flag.String("private-key", "", "DER RSA private key")
	flag.String("trust-certificate", "", "DER client certificate (gopcua does not validate client certificates)")
	arraySizes := flag.String("array-sizes", "", "comma-separated Int32 array lengths")
	runtimeInfo := flag.Bool("runtime-info", false, "print the runtime and SDK identity as JSON and exit")
	flag.Parse()

	if *runtimeInfo {
		info := map[string]string{"go": runtime.Version()}
		if build, ok := debug.ReadBuildInfo(); ok {
			for _, dependency := range build.Deps {
				if dependency.Path == "github.com/gopcua/opcua" {
					info["sdk"] = dependency.Version
				}
			}
		}
		return json.NewEncoder(os.Stdout).Encode(info)
	}
	if *port <= 0 || *port > 65535 {
		return errors.New("invalid --port")
	}
	sizes, err := parseSizes(*arraySizes)
	if err != nil {
		return err
	}

	// Package-level limits: the server ACK it offers and the largest array a
	// Variant may decode. Both default far below the largest payload shapes.
	uacp.DefaultServerACK.MaxMessageSize = maxMessageSize
	// gopcua enforces 0 as a limit of zero chunks, not as "no limit".
	uacp.DefaultServerACK.MaxChunkCount = maxMessageSize / uacp.DefaultReceiveBufSize * 2
	ua.MaxVariantArrayLength = 1 << 26

	options := []server.Option{
		server.EndPoint("127.0.0.1", *port),
		server.EnableAuthMode(ua.UserTokenTypeAnonymous),
		server.ServerName("o6 benchmark server"),
		server.ProductName("o6 benchmark"),
	}
	switch *security {
	case "None":
		options = append(options, server.EnableSecurity("None", ua.MessageSecurityModeNone))
	case "Basic256Sha256":
		certificatePath := firstNonEmpty(*certificate, os.Getenv("O6_BENCHMARK_CERTIFICATE"))
		keyPath := firstNonEmpty(*privateKey, os.Getenv("O6_BENCHMARK_PRIVATE_KEY"))
		if certificatePath == "" || keyPath == "" {
			return errors.New("encrypted runs require a certificate and a private key")
		}
		der, err := os.ReadFile(certificatePath)
		if err != nil {
			return err
		}
		key, err := loadKey(keyPath)
		if err != nil {
			return err
		}
		options = append(options,
			server.Certificate(der),
			server.PrivateKey(key),
			server.EnableSecurity("Basic256Sha256", ua.MessageSecurityModeSignAndEncrypt),
		)
	default:
		return errors.New("unknown --security value")
	}

	srv := server.New(options...)
	namespace := server.NewNodeNameSpace(srv, applicationURI)
	if namespace.ID() != 1 {
		return fmt.Errorf("benchmark namespace must be 1, got %d", namespace.ID())
	}
	root, err := srv.Namespace(0)
	if err != nil {
		return err
	}
	objects := root.Objects()
	add := func(node *server.Node) {
		namespace.AddNode(node)
		objects.AddRef(node, id.Organizes, true)
	}
	for index := 0; index < nodeCount; index++ {
		nodeID := uint32(firstNodeID + index)
		add(variable(namespace.ID(), nodeID, fmt.Sprintf("BenchmarkValue%d", index), int32(index), nil))
	}
	for index, size := range sizes {
		payload := make([]int32, size)
		for element := range payload {
			payload[element] = int32(element % 1000)
		}
		add(variable(namespace.ID(), uint32(arrayFirstNodeID+index), fmt.Sprintf("BenchmarkArray%d", size), payload, []uint32{uint32(size)}))
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := srv.Start(ctx); err != nil {
		return err
	}
	fmt.Printf("gopcua %s at opc.tcp://127.0.0.1:%d using #%s\n", serverReady, *port, *security)
	<-ctx.Done()
	return srv.Close()
}

func main() {
	log.SetFlags(0)
	log.SetOutput(os.Stderr)
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
