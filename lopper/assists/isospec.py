#/*
# * Copyright (c) 2023 Advanced Micro Devices, Inc. All Rights Reserved.
# *
# * Author:
# *       Bruce Ashfield <bruce.ashfield@amd.com>
# *
# * SPDX-License-Identifier: BSD-3-Clause
# */

import struct
import sys
import types
import unittest
import os
import getopt
import re
import subprocess
import shutil
from pathlib import Path
from pathlib import PurePath
from io import StringIO
import contextlib
import importlib
from lopper import Lopper
from lopper import LopperFmt
from lopper.yaml import LopperJSON
from lopper.tree import LopperAction
from lopper.tree import LopperTree
from lopper.tree import LopperNode
from lopper.tree import LopperProp
import lopper
import lopper_lib
from itertools import chain
import json
import humanfriendly

from lopper.log import _init, _warning, _info, _error, _debug
import logging

def is_compat( node, compat_string_to_test ):
    if re.search( "isospec,isospec-v1", compat_string_to_test):
        return isospec_domain
    if re.search( "module,isospec", compat_string_to_test):
        return isospec_domain
    return ""

# tests for a bit that is set, going fro 31 -> 0 from MSB to LSB
def check_bit_set(n, k):
    if n & (1 << (k)):
        return True

    return False

def set_bit(value, bit):
    return value | (1<<bit)

def clear_bit(value, bit):
    return value & ~(1<<bit)


def destinations( tree ):
    """returns all nodes with a destinations property in a tree
    """
    nodes_with_dests = []

    # find all the nodes with destinations in a tree. We are walking
    # all the nodes, and checking for a destinations property
    for n in tree:
        try:
            dests = n["destinations"]
            nodes_with_dests.append( n )
        except:
            pass

    return nodes_with_dests


iso_cpus_to_device_tree_map = {
                                "APU*": {
                                          "compatible": "arm,cortex-a72",
                                          "el": 3
                                        },
                                "RPU*": {
                                          "compatible": "arm,cortex-r5",
                                          "el": None
                                        }
                              }

def isospec_process_cpus( cpus_info, sdt, json_tree ):
    """ returns a list of dictionaries that represent the structure
        of any found cpus. These can be converted to json for future
        encoding into device tree properties
    """
    _info( f"isospec_process_cpus: {cpus_info} [{type(cpus_info)}] [{cpus_info}]" )

    cpus_list = []
    cpus = cpus_info["SMIDs"]
    for cpu_name in cpus:
        _info( f"    processing cpu: {cpu_name}" )

        cpu_map = {}
        for n,dn in iso_cpus_to_device_tree_map.items():
            if re.search( n, cpu_name ):
                cpu_map = dn

        if cpu_map:
            compat_string = cpu_map["compatible"]
            device_tree_compat = compat_string
        else:
            _error( f"unrecognized cpu {cpu_name}" )

        # did we have a mapped compatible string in the device tree ?
        if device_tree_compat:
            # is there a number in the isospec name ? If so, that is our
            # mask, if not, we set the cpu mask to 0x3 (them all)
            m = re.match( r'.*?(\d+)', cpu_name )
            if m:
                cpu_number = m.group(1)
            else:
                cpu_number = -1

            # look in the device tree for a node that matches the
            # mapped compatible string
            compatible_nodes = sdt.tree.cnodes( device_tree_compat )
            if compatible_nodes:
                # we need to find the cluster name / label, that's the parent
                # of the matching nodes, any node will do, so we take the first
                cpu_cluster = compatible_nodes[0].parent
                if not cpu_cluster:
                    _warning( f"no cluster found for cpus, returning" )
                    return None

                # take the label if set, otherwise take the node name
                cluster_name = cpu_cluster.label if cpu_cluster.label else cpu_cluster.name

                # we have the name, now we need the cluster mask. If
                # there's a cpu number. Confirm that the node exists,
                # and set the bit. If there's no number, our mask is
                # 0xf
                cluster_mask = 0
                if cpu_number != -1:
                    for c in compatible_nodes:
                        if re.search( "cpu@" + cpu_number, c.name ):
                            cluster_mask = set_bit( cluster_mask, int(cpu_number) )
                else:
                    cluster_mask = 0xf

                # cpu mode checks.
                #    secure
                #    el
                try:
                    cpu_flags = cpus_info["flags"]
                except:
                    cpu_flags = {}

                secure = False
                mode_mask = 0
                try:
                    secure_val = cpu_flags["secure"]
                    secure = secure_val
                except Exception as e:
                    pass

                try:
                    mode = cpu_flags["mode"]
                    if mode == "el":
                        mode_mask = set_bit( mode_mask, 0 )
                        mode_mask = set_bit( mode_mask, 1 )
                except:
                    # no passed mode, use the el level from the cpu_map
                    if cpu_map:
                        mode_mask = cpu_map["el"]

                if mode_mask:
                    cpu_entry = { "dev": cluster_name,    # remove before writing to yaml (if no roundtrip)
                                  "spec_name": cpu_name,  # rmeove before writing to yaml (if no roundtrip)
                                  "cluster" : cluster_name,
                                  "cpumask" : hex(cluster_mask),
                                  "mode" : { "secure": secure,
                                             "el": hex(mode_mask)
                                            }
                                 }
                else:
                    cpu_entry = { "dev": cluster_name,    # remove before writing to yaml (if no roundtrip)
                                  "spec_name": cpu_name,  # rmeove before writing to yaml (if no roundtrip)
                                  "cluster" : cluster_name,
                                  "cpumask" : hex(cluster_mask),
                                  "mode" : { "secure": secure }
                                 }

                cpus_list.append( cpu_entry )
        else:
            _warning( f"cpus entry {cpus_info[c]} has no device tree mapping" )

    _info( "cpus_list: %s" % cpus_list )

    return cpus_list

def isospec_device_flags( device_name, defs, json_tree ):

    domain_flag_dict = {}

    if type(defs) == dict:
        _info( f"isospec_device_flags: {defs}" )
        try:
            flags = defs["flags"]
            for flag,value in flags.items():
                if value:
                    domain_flag_dict[flag] = True
        except:
            return domain_flag_dict
    else:
        # try 1: is it a property ?
        flags = defs.propval( "flags" )

        # try 2: is it a subnode ?
        if not flags[0]:
            for n in defs.children():
                if n.name == "flags":
                    for p in n:
                        flags.append( p )

        # map the flags to something domains.yaml can output
        # create a flags dictionary, so we can next it into the access
        # structure below, which will then be transformed into yaml later.
        for flag in flags:
            try:
                if flag.value != '':
                    # if a flag is present, it means it was set to "true", it
                    # won't even be here in the false case.
                    domain_flag_dict[flag.name] = True
            except:
                pass

    _info( "isospec_device_flags: %s %s" % (device_name,domain_flag_dict) )

    return domain_flag_dict

# if something appears in this map, it is a memory entry, and
# we need to process it as such.
iso_memory_device_map = {
                          "DDR0" : ["memory", "memory@.*"],
                          "OCM.*" : ["sram", None]
                        }

def isospec_memory_type( name ):
    mem_found = None
    for n,v in iso_memory_device_map.items():
        if re.search( n, name ):
            mem_found = v

    if mem_found:
        return mem_found[0]

    return ""

def isospec_memory_dest( name ):
    mem_found = None
    for n,v in iso_memory_device_map.items():
        if re.search( n, name ):
            mem_found = v

    if mem_found:
        return mem_found[1]

    return ""

def isospec_process_memory( name, dest, sdt, json_tree, debug = False ):
    _info( f"isospec_process_memory: {dest}" )


    # xxxxxxxxxxxxxxxxxxxxxxxxxxxxx this is not picking up sram, even though the
    # xxxxxxxxxxxxxxxxxxxxxxxxxxxxx caller calls it sram and then places it on the
    # xxxxxxxxxxxxxxxxxxxxxxxxxxxxx sram list .. the values are wrong

    try:
        # is it explicitly tagged as memory ?
        mem_dest_flag = dest["mem"]
        memory_dest = "memory@.*"
        memory_type = "memory"
    except:
        # if it isn't, we have a regex match to figure
        # out what type of memory it may be
        memory_dest = isospec_memory_dest( name )
        memory_type = isospec_memory_type( name )
        if memory_type == "sram":
            print( "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd" )
            os._exit(1)

    possible_mem_nodes = []
    if memory_type == "memory":
        # we have a node to lookup in the device tree
        try:
            # Q: here's the problem. For SRAM this is called because
            #    there wasn't an address match when we looked up the
            #    memory. BUT, there are memory nodes in the tree, so
            #    this will return options. The SRAM may fall into one
            #    of the ranges, so we use that start/end address which
            #    then updates in the yaml. But that means the start
            #    address, etc, are lost (as is the type).
            #
            # Q: should we only declare a match if the start address
            #    matches, versus just being in the start + size of
            #    memory ? Only for SRAM or for all types of memory ?
            #
            possible_mem_nodes = sdt.tree.nodes(memory_dest)
            if debug:
                _info( f"possible mem nodes: {possible_mem_nodes}" )
        except Exception as e:
            _info( f"Exception looking for memory: {e}" )

    # if there's no possible device nodes, then we double
    # check the type mapping
    if not possible_mem_nodes:
        memory_dest = isospec_memory_dest( name )
        memory_type = isospec_memory_type( name )

    # Q: do we need to check if it is fully contained ?
    # We really only need the address for now, since we don't care if
    # this is fully contained in a memory range .. but we might in the
    # future if we don't end up adjusting the device tree, or if there
    # are multiple possible memory nodes, we could find the best fit.
    dest_start = int(dest['addr'],16)
    dest_size = dest['size']

    memory_node = None
    memory_list = []
    if memory_type == "memory":
        _info( f"  memory: {memory_dest}" )

        ##
        ## This may no longer be correct. But this is looking at the
        ## isospec memory, and seeing which system device tree nodes
        ## it may fall into. Those nodes are then used to create
        ## entries in the domains.yaml based on what we return here.
        ##
        ## If we are trying to adjust the SDT based on what is in the
        ## isospec, then this maybe not be correct. We need to
        ## clarify.
        ##
        memory_node_found=False
        for n in possible_mem_nodes:
            _info( f"  possible_mem_nodes: {n.abs_path} type: {n['device_type']}" )
            try:
                if "memory" in n["device_type"].value:
                    reg = n["reg"]
                    _info( f"    reg {reg.value}" )

                    # we could do this more generically and look it up
                    # in the parent, but 2 is the default, so doing
                    # this for initial effort
                    address_cells = 2
                    size_cells = 2

                    reg_chunks = lopper_lib.chunks( reg.value, address_cells + size_cells )
                    for reg_chunk in reg_chunks:
                        start = reg_chunk[0:address_cells]
                        start = lopper.base.lopper_base.encode_byte_array( start )
                        start = int.from_bytes(start,"big")

                        size =  reg_chunk[address_cells:]

                        size = lopper.base.lopper_base.encode_byte_array( size )
                        size = int.from_bytes(size,"big")

                        _info( f"    start: {hex(start)} size: {hex(size)}" )

                        ##
                        ## Q: Should we be checking if our address
                        ##    falls into this range ? or should be be
                        ##    checking if the range should be adjusted
                        ##    ? Something else ?
                        ##
                        ## Checking the range seems correct, and then
                        ## when we add this to domains.yaml, it will
                        ## adjust the device during final processing
                        ##
                        ## Without this, we get multiple mem entries
                        ## per isospec target, and that is not useful
                        ##
                        ## Q: Should the start/size be the memory
                        ##    start/size from the device tree, or from
                        ##    the isospec ?  if they aren't from the
                        ##    isospec, we don't have the information
                        ##    to adjust the output devie tree.
                        ##
                        if dest_start >= start and dest_start <= start + size:
                            _info( f"    memory is in range, adding: {name}" )
                            memory_node_found = True
                            memory_list.append( { "dev": name,          # remove before writing to yaml (if no roundtrip)
                                                  "spec_name": name,    # remove before writing to yaml (if no roundtrip)
                                                  "start": hex(start),
                                                  "size": hex(size)
                                                 }
                                               )

            except Exception as e:
                _debug( f"Exception {e}" )

        if not memory_node_found:
            # we could create one to match if this is the case, but for now, we warn.
            _warning( f"no memory node found that contains '{dest}'" )

    elif memory_type == "sram":
        # no memory dest
        _info( f"sram memory type: {memory_dest}" )
        address = dest['addr']
        tnode = sdt.tree.addr_node( address )
        if tnode:
            # pull the start and size out of the device tree node
            # don't have a device tree to test this yet
            _warning( f"    target node {tnode.abs_path} found, but no processing is implemented" )
        else:
            size = dest['size']
            # size = humanfriendly.parse_size( size, True )
            start = address
            _info( f"    sram start: {start} size: {size}" )
            memory_list.append( {
                                  "dev": name,        # remove before writing to yaml (if no roundtrip)
                                  "spec_name": name,  # remove before writing to yaml (if no roundtrip)
                                  "start": start,
                                  "size": size
                                }
                              )

    return memory_list

#### TODO: make this take a "type" and only return that type, versus the
####       current multiple list return
def isospec_process_access( access_node, sdt, json_tree, debug=False ):
    """processes the access values in an isospec subsystem
    """
    access_list = []
    memory_list = []
    sram_list = []
    cpu_list = []

    # access_node is a chunked json string
    _info( f"=======> isospec_process_access: {access_node}" )

    for a in range(len(access_node)):
        access = access_node[a]
        _info( f"process_access: {access}" )
        try:
            try:
                same_as_default = access["same_as_default"]
                _info( f"{access} has default settings for '{same_as_default}', looking up" )
                # same_as_default was set, we need to locate it
                defs = isospec_device_defaults( same_as_default, json_tree )
                if not defs:
                    _error( "cannot find default settings" )
            except:
                same_as_default = None
                # inline values: "destinations" : [ list of dests ]
                defs = access

            _info( f"found device defaults: {defs}", defs )

            # look at the type of access. that dictates where we find
            # the destinations / target.
            try:
                access_type = defs["type"]
            except:
                access_type = "device"

            if access_type == "cpu_list":
                iso_cpus = defs["SMIDs"]
                iso_cpu_list = isospec_process_cpus( defs, sdt, json_tree )
                cpu_list.extend( iso_cpu_list )
                _info( f"isospec_process_access: cpus list collected: {cpu_list}")
            elif access_type == "device":
                _info( f"ispospec_process_actions: device with destinations: {defs['destinations']}" )

                flag_mapping = isospec_device_flags( defs["name"], defs, json_tree )
                try:
                    device_requested = flag_mapping["requested"]
                except:
                    _info( f'device \"{defs["name"]}\" was found, but not requested. adding to domain' )

                # find the destinations in the isospec json tree
                dests = isospec_device_destination( defs["destinations"], json_tree )
                
                # we now need to locate the destination device in the device tree, all
                # we have is the address to use for the lookup
                for d in dests:
                    _info( f"isospec_process_access: ----> prcessing destination: {d['name']}" )
                    try:
                        ## Q: what should we do with entries that are tagged as "mem", but
                        ##    we find a matchig node by address ? Should they be the device
                        ##    or be added as a device ?
                        address = d['addr']
                        name = d['name']
                        _info( f"    {d['name']}: checking for device tree matching address {address}" )
                        tnode = sdt.tree.addr_node( address )
                        if tnode:
                            _info( f"      {d['name']}: found node at address {address}: {tnode}" )
                            access_list.append( {
                                                  "dev": tnode.name,
                                                  "spec_name": name,
                                                  "label": tnode.label,
                                                  "flags": flag_mapping
                                                }
                                              )
                        else:
                            raise Exception( f"No node found for {name} => {d}" )
                    except Exception as e:
                        _info( f"    memory: checking dest {d['name']} [{e}]" )
                        mem_found = None
                        try:
                            # does the target have "mem": True ? if so, then we definitely
                            # have memory. If not, we check for the mapping of memory names
                            # defined in the iso_memory_device_map dictionary against the
                            # name of the dest.
                            mem_found = d["mem"]

                            # if we are here, then the destination didn't have a node
                            # in the device tree we could match, so we can use our
                            # name mapping to decide if it is sram or memory
                            memory_type = isospec_memory_type(d['name'])
                            _info( f"    memory with no node, returned type: {memory_type}" )
                            if not memory_type:
                                memory_type = "memory"
                        except:
                            _info( f"    regex test of checking dest {d['name']} as memory" )
                            memory_type = isospec_memory_type(d['name'])

                            # this should be calling isospec_memory_dest() ...
                            for n,v in iso_memory_device_map.items():
                                if re.search( n, d['name'] ):
                                    mem_found = v

                        # no warning if we failed on memory in this try clause
                        if mem_found:
                            _info( f"    dest {d['name']} identified as memory of type: {memory_type}" )

                            debug = False
                            if memory_type == "sram":
                                debug = True

                            # Q: the memory question is outstanding, what to do when memory
                            #    falls within a device tree range.
                            ml = isospec_process_memory( d['name'], d, sdt, json_tree, debug )

                            #if memory_type == "sram":
                            #    os._exit(1)

                            if memory_type == "memory":
                                memory_list.extend( ml )
                            if memory_type == "sram":
                                sram_list.extend( ml )

                            # no warning for memory
                            continue

                        # it was something other than a dict returned as a dest
                        _warning( f"isospec: process_access: {e}" )

        except Exception as e:
            pass

    return access_list, cpu_list, memory_list, sram_list

def isospec_device_defaults( device_name, isospec_json_tree ):
    """
    returns the default settings for the named device
    """

    default_settings = isospec_json_tree["/default_settings"]
    if not default_settings:
        return None

    default_subsystems = isospec_json_tree["/default_settings/subsystems"]
    if not default_subsystems:
        return None

    default_subsystem = None
    for s in default_subsystems.children():
        if s.name == "default":
            default_subsystem = s

    # _info( " default settings, default subsystem found!" )
    if not default_subsystem:
        return None

    ### Note: we should probably be matching up the "id" that is part
    ### of this subsystem the requestor, since not all
    ### "same_as_default" values must be in the subsysystem named
    ### "default"

    # we now (finally) have the default subsystem. The subnodes and
    # properties of this node contain our destinations with default
    # values for the various settings

    # if we end up with large domains, we may want to run this once
    # and construct a dictionary to consult later.

    try:
        default_access = default_subsystem["access"]
        access_list = []
        for d in range(len(default_access)):
            access_list.append( default_access[d] )

        device_default = [d for d in access_list if d["name"] == device_name][0]
    except Exception as e:
        # no settings, return none
        _info( f"exception while doing default settings {e}" )
        return None

    return device_default

def isospec_device_destination( destination_list, isospec_json_tree ):
    """Look for the isospec "destinations" that match the passed
       list of destinations.

       returns a list of the isospec destinatino that matches
    """

    destination_result = []

    # locate all nodes in the tree that have a destinations property
    dnodes = destinations( isospec_json_tree )

    for destination in destination_list:
        for n in dnodes:
            try:
                dests = n["destinations"]
            except Exception as e:
                pass

            if dests.pclass == "json":
                _debug( f"node {n.abs_path} has json destinations property: {dests.name}" )
                # _info( f"raw dests: {dests.value} ({type(dests.value)})" )
                try:
                    for i in range(len(dests)):
                        x = dests[i]
                        if x["name"] == destination:
                            destination_result.append( x )
                except Exception as e:
                    # it wsn't a dict, ignore
                    pass
            else:
                pass
                # for i in dests.value:
                #     if i == destination:
                #         destination_result.append( i )

    _info( f"destinations found: {destination_result}" )

    return destination_result

def domains_tree_start():
    """ Start a device tree to represent a system device tree domain
    """
    domains_tree = LopperTree()
    domain_node = LopperNode( abspath="/domains", name="domains" )

    return domains_tree

def domains_tree_add_subsystem( domains_tree, subsystem_name="default-subsystem", subsystem_id=0 ):

    subsystems_node = LopperNode( abspath=f"/domains/{subsystem_name}", name=subsystem_name )
    subsystems_node["compatible"] = "xilinx,subsystem"
    subsystems_node["id"] = subsystem_id
    domains_tree = domains_tree + subsystems_node

    return domains_tree

def domains_tree_add_domain( domains_tree, domain_name="default", parent_domain = None, id=0 ):

    if not parent_domain:
        domain_node = LopperNode( abspath=f"/domains/{domain_name}", name=domain_name )
        domain_node["compatible"] = "openamp,domain-v1"
        domain_node["id"] = id
        domains_tree = domains_tree + domain_node
    else:
        domain_node = LopperNode( name=domain_name )
        domain_node["compatible"] = "openamp,domain-v1"
        domain_node["id"] = id
        parent_domain + domain_node

    return domain_node

def process_domain( domain_node, iso_node, json_tree, sdt ):
    _info( f"infospec_domain: process_domain: processing: {iso_node.name}" )
    # iso_node.print()

    debug = False

    # access and memory AND now cpus
    try:
        iso_access = json_tree[f"{iso_node.abs_path}"]["access"]
        _info( f"access: {iso_access}" )

        access_list,cpus_list,memory_list,sram_list = isospec_process_access( iso_access, sdt, json_tree, debug )
        if cpus_list:
            domain_node["cpus"] = json.dumps(cpus_list)
            domain_node.pclass = "json"
        if memory_list:
            _info( f"memory: {memory_list}" )
            domain_node["memory"] = json.dumps(memory_list)
        if sram_list:
            _info( f"sram: {memory_list}" )
            domain_node["sram"] = json.dumps(sram_list)

        domain_node["access"] = json.dumps(access_list)
    except KeyError as e:
        _error( f"no access list in {iso_node.abs_path}" )
    except Exception as e:
        _error( f"problem during subsystem processing: {e}" )

    return domain_node

#
# This collects all of the possible devices in the isospec
# into a dictionary.
#
# That dictionarty is indexed by the name of the device
# and points to a dictionary that contains a reference count
# and the definition in the spec (the destination line)
#
def device_collect( isospec_json_tree ):
    device_dict = {}
    _info( f"collecting alll possible devices" )
    try:
        design_cells = isospec_json_tree["/design/cells"]
    except:
        _warning( "no design/cells found in isolation spec" )
        return device_dict

    for cell in design_cells.children():
        try:
            dests = cell["destinations"]
            _debug( f"processing cell: {cell.name}" )
            _debug( f"           destinations {dests.abs_path} [{len(dests)}]" )
            for d in range(len(dests)):
                dest = dests[d]
                _debug( f"                dest: {dest}" )
                # A device has to have a nodeid for us to consider it, since
                # otherwise it can't be referenced. The exception to this is
                # memory, since memory entries never have nodeids.
                try:
                    nodeid = dest["nodeid"]
                    device_dict[dest["name"]] = {
                                                  "refcount": 0,
                                                  "dest": dest
                                                 }
                except:
                    try:
                        is_it_mem = dest["mem"]
                    except:
                        is_it_mem = False
                        True

                    # this may be controlled by a command line option
                    # in the future
                    skip_memory = False

                    ## Q: We need to decide if memory always shows up in the global
                    ##    device list, even without a nodeid.
                    if is_it_mem:
                        if not skip_memory:
                            device_dict[dest["name"]] = {
                                "refcount": 0,
                                "dest": dest
                            }
                        else:
                            _debug( "                memory detected (skipping (no nodeid))" )
                    else:
                        # no nodeid, skip
                        _debug( "                   ** destination has no nodeid, skipping" )
        except:
            True

    return device_dict


def isospec_domain( tgt_node, sdt, options ):
    """assist entry point, called from lopper when a node is
       identified, or passed as a command line assist
    """
    try:
        verbose = options['verbose']
    except:
        verbose = 0

    try:
        args = options['args']
    except:
        args = []

    lopper.log._init( __name__ )

    opts,args2 = getopt.getopt( args, "mpvh", [ "help", "verbose", "permissive", "nomemory" ] )

    if opts == [] and args2 == []:
        usage()
        sys.exit(1)

    memory = True
    for o,a in opts:
        # print( "o: %s a: %s" % (o,a))
        if o in ('-m', "--nomemory" ):
            memory = False
        elif o in ('-v', "--verbose"):
            verbose = verbose + 1
        elif o in ('-c', "--compare"):
            compare_list.append( a )
        elif o in ('-p', "--permissive"):
            permissive = True
        elif o in ('-h', "--help"):
            # usage()
            sys.exit(1)

    if verbose:
        lopper.log._level( logging.INFO, __name__ )
    if verbose > 1:
        lopper.log._level( logging.DEBUG, __name__ )
        #logging.getLogger().setLevel( level=logging.DEBUG )

    _info( f"cb: isospec_domain( {tgt_node}, {sdt}, {verbose} )" )

    if sdt.support_files:
        isospec = sdt.support_files.pop()
    else:
        try:
            if not args2[0]:
                _error( "isospec: no isolation specification passed" )
            isospec = args2.pop(0)
        except Exception as e:
            _error( f"isospec: no isolation specification passed: {e}" )
            sys.exit(1)

    domain_yaml_file = "domains.yaml"
    try:
        domain_yaml_file = args2.pop(0)
    except:
        pass

    try:
        iso_file = Path( isospec )
        iso_file_abs = iso_file.resolve( True )
    except FileNotFoundError as e:
        _error( f"ispec file {isospec} not found" )

    # convert the spec to a LopperTree for consistent manipulation
    json_in = LopperJSON( json=iso_file_abs )
    json_tree = json_in.to_tree()

    # TODO: make the tree manipulations and searching a library function
    domains_tree = domains_tree_start()
    iso_subsystems = json_tree["/design/subsystems"]
    try:
        iso_domains = json_tree["/design/subsystems/" ]
    except:
        pass
    
    device_dict = device_collect( json_tree )

    # iso_subsystems.print()
    # print( iso_subsystems.children() )

    for iso_node in iso_subsystems.children():
        isospec_domain_node = json_tree[f"{iso_node.abs_path}"]

        domain_id = iso_node["id"]
        domain_node = domains_tree_add_domain( domains_tree, iso_node.name, None, domain_id )

        ## these are subsystems, which have nested domains
        domain_node = process_domain( domain_node, iso_node, json_tree, sdt )

        # add the domain to the domains tree, and process any subdomains
        try:
            sub_domains = json_tree[f"{iso_node.abs_path}" + "/domains"]
            # sub_domains.print()
            sub_domain_node = LopperNode( name="domains" )
            domain_node = domain_node + sub_domain_node
            for s in sub_domains.children():
                try:
                    domain_id = s["id"]
                except:
                    # copy the subsystem's id
                    domain_id = iso_node["id"]
                sub_domain_node_new = domains_tree_add_domain( domains_tree, s.name, sub_domain_node, domain_id )
                sub_domain_node_new = process_domain( sub_domain_node_new, s, json_tree, sdt )
                # domain_node.print()
        except:
            pass

    # gather a list of all created domains, so we can avoid this check
    # in the upcoming code.
    domains_list = []
    for domain in domains_tree:
        try:
            compat = domain["compatible"].value
            if compat == "openamp,domain-v1":
                # this is a valid domain, add it to the list
                domains_list.append( domain )
        except:
            # if it isn't a domain, skip
            True

    #
    # Global accounting based on what we found in the domain
    # processing. This is currently just a reference count for
    # anything in the access list of the domain.
    #
    # Note/TODO: It should be possible to assign a pclass of json
    #            to the access_property, and avoid the explicit
    #            json loading and chunking below.
    #
    # Note: It might be possible to do this when processing the
    #       domains, to optimize the processing time.
    #
    # This currently checks the "access", "memory" and "sram"
    # lists (and possibly "cpus" in the future)
    #
    for domain in domains_list:            
        for domain_ref_type in [ "access", "memory", "sram" ]:
            _info( f"refcounting domain: '{domain_node.name}' type: {domain_ref_type}" )
            try:
                domain_json = json.loads(domain_node[domain_ref_type].value)
            except Exception as e:
                _info( f"no entries found .. skipping" )
                continue

            _debug( f"{iso_node.abs_path} [{len(domain_json)}] list: {domain_json}" )
            for v in range(len(domain_json)):
                dev = domain_json[v]
                _info( f"   device: {dev}" )
                # check the device dict
                try:
                    # this throws an exception if the device wasn't
                    # setup as something we are tracking
                    gdevice = device_dict[dev["spec_name"]]
                    # increment the refcount
                    gdevice["refcount"] += 1
                    _info( f"      tracked element: {gdevice}: refcounted" )
                except:
                    _info( f"      element: {gdevice} is not a tracked" )



    _info( "Unreferenced device processing" )
    for d,device in device_dict.items():
        if device["refcount"] == 0:
            _info( f"    unreferenced device: {device}" )

            unrefed_device_entry = {}
            unrefed_memory_entry = {}

            # We won't find memory, sram or cpus in the device
            # tree (tnode will be empty), so they won't get
            # added to the access list.  They will be added as
            # umapped memory (to all domains).
            try:
                tnode = sdt.tree.addr_node( device["dest"]["addr"] )
            except:
                # If the device has no address, consider it as memory ?
                tnode = None
            if tnode:
                _info( f"      device tree node '{tnode.name}' found at address: {device['dest']['addr']}" )

                name = device["dest"]['name']
                unrefed_device_entry = {
                                          "dev": tnode.name,     # remove before writing to yaml (if no roundtrip)
                                          "spec_name": name,     # remove before writing to yaml (if no roundtrip)
                                          "label": tnode.label,
                                          "flags": {}
                                       }
            else:
                # was it memory ?
                try:
                    ##
                    ## Q: do we need to handle SRAM ?
                    ##

                    ## 
                    ## This may become conditional with an isospec
                    ## command line option.
                    ##

                    # This throws an exception if we aren't memory and
                    # we skip all the rest of the processing
                    is_it_memory = device["dest"]["mem"]
                    name = device["dest"]["name"]
                    start = device["dest"]["addr"]
                    size = device["dest"]["size"]
                    _info( f"      unreferenced memory detected: {device['dest']['name']}" )
                    unrefed_memory_entry = { "dev": name,          # remove before writing to yaml (if no roundtrip)
                                             "spec_name": name,    # remove before writing to yaml (if no roundtrip)
                                             "start": start,
                                             "size": size
                                           }
                except Exception as e:
                    _debug( f"    unreferenced device {device} has no device tree node, and is not memory. skipping" )

            # Unreferenced devices/memory are added to all domains
            for domain in domains_list:
                if unrefed_device_entry:
                    _info( f"        [%s] adding device: %s" % (domain.name,device["dest"] ))
                    access_json = json.loads(domain["access"].value)
                    access_json.append( unrefed_device_entry )
                    domain["access"] = json.dumps(access_json)

                if unrefed_memory_entry:
                    _info( f"        [%s] adding memory: %s" % (domain.name,device["dest"] ))
                    try:
                        memory_json = json.loads(domain["memory"].value)
                    except:
                        memory_json = []

                    memory_json.append( unrefed_memory_entry)
                    domain["memory"] = json.dumps(memory_json)
            
    _info( f"unreferenced device processing complete" )

    # domains_tree.print()

    # write the yaml tree
    _info( f"writing domain file: {domain_yaml_file}" )
    sdt.write( domains_tree, output_filename=domain_yaml_file, overwrite=True )

    return True

