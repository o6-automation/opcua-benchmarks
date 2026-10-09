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

/* The S2OPC benchmark server: the shared address space of
 * common/servers/open62541_server.c, served by the S2OPC toolkit.
 *
 * Namespace 0 comes from S2OPC's own base NodeSet (loaded with its Expat
 * loader); the benchmark variables are appended in C, so a large array never
 * goes through XML. S2OPC requires namespace 1 to be the application URI, so
 * the variables live there, which is where every other server puts them. */

#include "common/contract.h"

#include <signal.h>
#include <stdatomic.h>
#include <unistd.h>

#include "libs2opc_common_config.h"
#include "libs2opc_server.h"
#include "libs2opc_server_config.h"
#include "libs2opc_server_config_custom.h"
#include "sopc_address_space.h"
#include "sopc_assert.h"
#include "sopc_crypto_profiles.h"
#include "sopc_helper_string.h"
#include "sopc_mem_alloc.h"
#include "sopc_pki_stack.h"
#include "sopc_uanodeset_loader.h"
#include "sopc_user_manager.h"

#define APPLICATION_URI "urn:o6:benchmark:server"
#define OPCUA_URI "http://opcfoundation.org/UA/"

static atomic_int stop_requested = 0;
static atomic_int server_stopped = 0;

static void on_signal(int signal_number) {
    (void)signal_number;
    stop_requested = 1;
}

static void on_server_stopped(SOPC_ReturnStatus status) {
    (void)status;
    server_stopped = 1;
}

static int load_file(const char *path, unsigned char **data, size_t *length) {
    FILE *file = path ? fopen(path, "rb") : NULL;
    long size;
    *data = NULL;
    *length = 0;
    if(!file)
        return 0;
    if(fseek(file, 0, SEEK_END) != 0 || (size = ftell(file)) <= 0 || fseek(file, 0, SEEK_SET) != 0) {
        fclose(file);
        return 0;
    }
    *data = malloc((size_t)size);
    if(*data && fread(*data, 1, (size_t)size, file) == (size_t)size)
        *length = (size_t)size;
    fclose(file);
    return *length > 0;
}

static int parse_size_list(const char *text, size_t *values, size_t capacity, size_t *count) {
    const char *cursor = text;
    *count = 0;
    while(cursor && *cursor != '\0') {
        char *end = NULL;
        unsigned long long parsed;
        size_t existing;
        int duplicate = 0;
        errno = 0;
        parsed = strtoull(cursor, &end, 10);
        if(errno != 0 || end == cursor || parsed == 0)
            return 0;
        for(existing = 0; existing < *count; ++existing)
            duplicate |= values[existing] == (size_t)parsed;
        if(!duplicate) {
            if(*count >= capacity)
                return 0;
            values[(*count)++] = (size_t)parsed;
        }
        cursor = end;
        if(*cursor == ',')
            ++cursor;
        else if(*cursor != '\0')
            return 0;
    }
    return 1;
}

/* Fill a String array variable that S2OPC's base NodeSet leaves empty. */
static SOPC_ReturnStatus set_string_array(SOPC_AddressSpace *space, uint32_t id,
                                          const char **values, int32_t count) {
    SOPC_NodeId node_id = {.IdentifierType = SOPC_IdentifierType_Numeric, .Namespace = 0, .Data.Numeric = id};
    bool found = false;
    SOPC_AddressSpace_Node *node = SOPC_AddressSpace_Get_Node(space, &node_id, &found);
    SOPC_Variant *value;
    int32_t index;
    if(!found || !node)
        return SOPC_STATUS_NOK;
    value = SOPC_AddressSpace_Get_Value(space, node);
    SOPC_Variant_Clear(value);
    value->BuiltInTypeId = SOPC_String_Id;
    value->ArrayType = SOPC_VariantArrayType_Array;
    value->Value.Array.Content.StringArr = SOPC_Calloc((size_t)count, sizeof(SOPC_String));
    if(!value->Value.Array.Content.StringArr)
        return SOPC_STATUS_OUT_OF_MEMORY;
    value->Value.Array.Length = count;
    for(index = 0; index < count; ++index) {
        SOPC_String_Initialize(&value->Value.Array.Content.StringArr[index]);
        if(SOPC_String_CopyFromCString(&value->Value.Array.Content.StringArr[index], values[index]) != SOPC_STATUS_OK)
            return SOPC_STATUS_OUT_OF_MEMORY;
    }
    return SOPC_STATUS_OK;
}

static SOPC_ReturnStatus add_variable(SOPC_AddressSpace *space, uint32_t id, const char *name,
                                      SOPC_Variant *value, size_t array_size) {
    SOPC_AddressSpace_Node *node = SOPC_Calloc(1, sizeof(SOPC_AddressSpace_Node));
    OpcUa_VariableNode *variable;
    OpcUa_ReferenceNode *references;
    SOPC_ReturnStatus status = SOPC_STATUS_OK;
    if(!node)
        return SOPC_STATUS_OUT_OF_MEMORY;
    SOPC_AddressSpace_Node_Initialize(space, node, OpcUa_NodeClass_Variable);
    node->value_status = SOPC_GoodGenericStatus;
    variable = &node->data.variable;
    variable->NodeClass = OpcUa_NodeClass_Variable;
    variable->NodeId.IdentifierType = SOPC_IdentifierType_Numeric;
    variable->NodeId.Namespace = 1;
    variable->NodeId.Data.Numeric = id;
    variable->BrowseName.NamespaceIndex = 1;
    status = SOPC_String_CopyFromCString(&variable->BrowseName.Name, name);
    if(status == SOPC_STATUS_OK)
        status = SOPC_String_CopyFromCString(&variable->DisplayName.defaultText, name);
    variable->DataType.IdentifierType = SOPC_IdentifierType_Numeric;
    variable->DataType.Namespace = 0;
    variable->DataType.Data.Numeric = SOPC_Int32_Id;
    variable->AccessLevel = 0x03; /* CurrentRead | CurrentWrite */
    variable->UserAccessLevel = 0x03;
    variable->ValueRank = array_size ? 1 : -1;
    if(array_size && status == SOPC_STATUS_OK) {
        variable->ArrayDimensions = SOPC_Calloc(1, sizeof(uint32_t));
        if(!variable->ArrayDimensions)
            status = SOPC_STATUS_OUT_OF_MEMORY;
        else {
            variable->NoOfArrayDimensions = 1;
            variable->ArrayDimensions[0] = (uint32_t)array_size;
        }
    }
    references = SOPC_Calloc(2, sizeof(OpcUa_ReferenceNode));
    if(!references)
        status = SOPC_STATUS_OUT_OF_MEMORY;
    if(status == SOPC_STATUS_OK) {
        OpcUa_ReferenceNode_Initialize(&references[0]);
        OpcUa_ReferenceNode_Initialize(&references[1]);
        references[0].ReferenceTypeId.Data.Numeric = 40; /* HasTypeDefinition */
        references[0].TargetId.NodeId.Data.Numeric = 63; /* BaseDataVariableType */
        references[1].ReferenceTypeId.Data.Numeric = 35; /* Organizes, from ObjectsFolder */
        references[1].IsInverse = true;
        references[1].TargetId.NodeId.Data.Numeric = 85;
        variable->NoOfReferences = 2;
        variable->References = references;
        variable->Value = *value; /* ownership moves to the node */
        SOPC_Variant_Initialize(value);
        status = SOPC_AddressSpace_Append(space, node);
    }
    if(status != SOPC_STATUS_OK)
        SOPC_AddressSpace_Node_Delete(space, node);
    return status;
}

static SOPC_ReturnStatus build_address_space(const char *nodeset, const size_t *array_sizes,
                                             size_t array_size_count) {
    static const char *namespaces[] = {OPCUA_URI, APPLICATION_URI};
    static const char *servers[] = {APPLICATION_URI};
    FILE *file = fopen(nodeset, "r");
    SOPC_AddressSpace *space;
    SOPC_ReturnStatus status;
    uint32_t index;
    if(!file) {
        fprintf(stderr, "Cannot open base NodeSet %s\n", nodeset);
        return SOPC_STATUS_NOK;
    }
    space = SOPC_UANodeSet_Parse(file);
    fclose(file);
    if(!space)
        return SOPC_STATUS_NOK;
    status = set_string_array(space, 2255, namespaces, 2); /* Server_NamespaceArray */
    if(status == SOPC_STATUS_OK)
        status = set_string_array(space, 2254, servers, 1); /* Server_ServerArray */
    for(index = 0; status == SOPC_STATUS_OK && index < O6_NODE_COUNT; ++index) {
        char name[32];
        SOPC_Variant value;
        SOPC_Variant_Initialize(&value);
        value.BuiltInTypeId = SOPC_Int32_Id;
        value.ArrayType = SOPC_VariantArrayType_SingleValue;
        value.Value.Int32 = (int32_t)index;
        snprintf(name, sizeof(name), "BenchmarkValue%u", (unsigned)index);
        status = add_variable(space, O6_FIRST_NODE_ID + index, name, &value, 0);
    }
    for(index = 0; status == SOPC_STATUS_OK && index < array_size_count; ++index) {
        char name[48];
        size_t element;
        SOPC_Variant value;
        SOPC_Variant_Initialize(&value);
        value.BuiltInTypeId = SOPC_Int32_Id;
        value.ArrayType = SOPC_VariantArrayType_Array;
        value.Value.Array.Content.Int32Arr = SOPC_Calloc(array_sizes[index], sizeof(int32_t));
        if(!value.Value.Array.Content.Int32Arr) {
            status = SOPC_STATUS_OUT_OF_MEMORY;
            break;
        }
        value.Value.Array.Length = (int32_t)array_sizes[index];
        for(element = 0; element < array_sizes[index]; ++element)
            value.Value.Array.Content.Int32Arr[element] = (int32_t)(element % 1000);
        snprintf(name, sizeof(name), "BenchmarkArray%zu", array_sizes[index]);
        status = add_variable(space, O6_ARRAY_FIRST_NODE_ID + index, name, &value, array_sizes[index]);
        SOPC_Variant_Clear(&value);
    }
    if(status == SOPC_STATUS_OK)
        status = SOPC_ServerConfigHelper_SetAddressSpace(space);
    if(status != SOPC_STATUS_OK)
        SOPC_AddressSpace_Delete(space);
    return status;
}

int main(int argc, char **argv) {
    size_t array_sizes[O6_MAX_ARRAY_SIZES];
    size_t array_size_count = 0;
    unsigned long port = 4840;
    const char *security = "None";
    const char *certificate_path = getenv("O6_BENCHMARK_CERTIFICATE");
    const char *key_path = getenv("O6_BENCHMARK_PRIVATE_KEY");
    const char *nodeset = NULL;
    char url[64];
    int argument;
    SOPC_ReturnStatus status;
    SOPC_Log_Configuration log_config;
    SOPC_Endpoint_Config *endpoint;
    SOPC_SecurityConfig *policy;
    SOPC_PKIProvider *pki = NULL;
    bool secure;

    for(argument = 1; argument < argc; ++argument) {
        const char *flag = argv[argument];
        const char *value = argument + 1 < argc ? argv[argument + 1] : NULL;
        if(!value) {
            fprintf(stderr, "%s needs a value\n", flag);
            return EXIT_FAILURE;
        }
        ++argument;
        if(strcmp(flag, "--port") == 0) {
            char *end = NULL;
            port = strtoul(value, &end, 10);
            if(!end || *end != '\0' || port == 0 || port > 65535) {
                fprintf(stderr, "Invalid --port value\n");
                return EXIT_FAILURE;
            }
        } else if(strcmp(flag, "--security") == 0) {
            security = value;
            if(strcmp(security, "None") != 0 && strcmp(security, "Basic256Sha256") != 0) {
                fprintf(stderr, "Unknown --security value\n");
                return EXIT_FAILURE;
            }
        } else if(strcmp(flag, "--certificate") == 0) {
            certificate_path = value;
        } else if(strcmp(flag, "--private-key") == 0) {
            key_path = value;
        } else if(strcmp(flag, "--trust-certificate") == 0) {
            /* Like the open62541 server, any client certificate is accepted. */
        } else if(strcmp(flag, "--nodeset") == 0) {
            nodeset = value;
        } else if(strcmp(flag, "--array-sizes") == 0) {
            if(!parse_size_list(value, array_sizes, O6_MAX_ARRAY_SIZES, &array_size_count)) {
                fprintf(stderr, "Invalid --array-sizes list\n");
                return EXIT_FAILURE;
            }
        } else {
            fprintf(stderr, "Unknown argument %s\n", flag);
            return EXIT_FAILURE;
        }
    }
    if(!nodeset) {
        fprintf(stderr, "--nodeset (S2OPC base NodeSet) is required\n");
        return EXIT_FAILURE;
    }
    secure = strcmp(security, "Basic256Sha256") == 0;

    log_config = SOPC_Common_GetDefaultLogConfiguration();
    log_config.logLevel = SOPC_LOG_LEVEL_ERROR;
    log_config.logSystem = SOPC_LOG_SYSTEM_NO_LOG;
    status = SOPC_CommonHelper_Initialize(&log_config, NULL);
    if(status == SOPC_STATUS_OK)
        status = SOPC_ServerConfigHelper_Initialize();
    if(status == SOPC_STATUS_OK)
        status = SOPC_ServerConfigHelper_SetApplicationDescription(
            APPLICATION_URI, "urn:o6:benchmark", "o6 benchmark server", NULL, OpcUa_ApplicationType_Server);

    snprintf(url, sizeof(url), "opc.tcp://127.0.0.1:%lu", port);
    endpoint = status == SOPC_STATUS_OK ? SOPC_ServerConfigHelper_CreateEndpoint(url, true) : NULL;
    policy = endpoint ? SOPC_EndpointConfig_AddSecurityConfig(
                            endpoint, secure ? SOPC_SecurityPolicy_Basic256Sha256 : SOPC_SecurityPolicy_None)
                      : NULL;
    if(!policy)
        status = SOPC_STATUS_NOK;
    if(status == SOPC_STATUS_OK)
        status = SOPC_SecurityConfig_SetSecurityModes(
            policy, secure ? SOPC_SecurityModeMask_SignAndEncrypt : SOPC_SecurityModeMask_None);
    if(status == SOPC_STATUS_OK)
        status = SOPC_SecurityConfig_AddUserTokenPolicy(policy, &SOPC_UserTokenPolicy_Anonymous);

    if(status == SOPC_STATUS_OK && secure) {
        unsigned char *certificate = NULL;
        unsigned char *key = NULL;
        size_t certificate_length = 0;
        size_t key_length = 0;
        if(!load_file(certificate_path, &certificate, &certificate_length) ||
           !load_file(key_path, &key, &key_length)) {
            fprintf(stderr, "Encrypted runs require a certificate and a private key\n");
            status = SOPC_STATUS_NOK;
        } else {
            status = SOPC_ServerConfigHelper_SetKeyCertPairFromBytes(certificate_length, certificate,
                                                                     key_length, key);
        }
        free(certificate);
        free(key);
    }
    if(status == SOPC_STATUS_OK)
        status = SOPC_PKIPermissive_Create(&pki);
    if(status == SOPC_STATUS_OK)
        status = SOPC_ServerConfigHelper_SetPKIprovider(pki);
    if(status == SOPC_STATUS_OK)
        status = SOPC_ServerConfigHelper_SetUserAuthenticationManager(
            SOPC_UserAuthentication_CreateManager_AllowAll());
    if(status == SOPC_STATUS_OK)
        status = SOPC_ServerConfigHelper_SetUserAuthorizationManager(
            SOPC_UserAuthorization_CreateManager_AllowAll());
    if(status == SOPC_STATUS_OK)
        status = build_address_space(nodeset, array_sizes, array_size_count);

    if(status == SOPC_STATUS_OK) {
        signal(SIGINT, on_signal);
        signal(SIGTERM, on_signal);
        status = SOPC_ServerHelper_StartServer(on_server_stopped);
    }
    if(status == SOPC_STATUS_OK) {
        printf("S2OPC %s at %s using #%s\n", O6_SERVER_READY, url, security);
        fflush(stdout);
        while(!stop_requested && !server_stopped)
            usleep(50 * 1000);
        if(!server_stopped)
            SOPC_ServerHelper_StopServer();
        while(!server_stopped)
            usleep(10 * 1000);
    } else {
        fprintf(stderr, "S2OPC server setup failed (status %d)\n", (int)status);
    }
    SOPC_ServerConfigHelper_Clear();
    SOPC_CommonHelper_Clear();
    return status == SOPC_STATUS_OK ? EXIT_SUCCESS : EXIT_FAILURE;
}
