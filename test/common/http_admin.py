# This file contains several classes used for running a rethinkdb cluster in different states
#
# Cluster - This base class provides most functionality for managing a cluster:
#   add_machine - adds a new instance of rethinkdb by instantiating an InternalServer
#   add_datacenter - adds a new datacenter object to the cluster
#   add_namespace - adds a new namespace of the specified type (dummy or memcached)
#   kill_machines - kills a rethinkdb_server, but does not clean up its metadata, it may be recovered later to rejoin the cluster (UNTESTED)
#   recover_machines - recovers a previously killed rethinkdb_server, which should then rejoin the cluster
# The cluster's machines, datacenters, and *_namespaces variables may be inspected directly, but
#   should not be touched.  The cluster is specialized into an ExternalCluster and InternalCluster
# ExternalCluster - This is deprecated, and will probably be removed.  The original intention was to
#   allow the script to connect to a cluster that was instantiated outside of this script
# InternalCluster - This is the typical type with which a cluster should be instantiated.  This constructor
#   will instantiate a cluster with the given number of datacenters and namespaces.  InternalClusters
#   also allow for the cluster to be split and joined.
#
# InternalCluster::split - this will split off a set of machines or an arbitrary number of machines from
#   the cluster and return another InternalCluster object.  Methods may then be performed on only one
#   of the remaining sub-clusters.  In order to use this method, the "resunder" daemon must be started.
#   This can be done using "resunder.py" in the same directory as this file.  "sudo resunder.py start" and
#   "./resunder.py stop" to start and stop the daemon.  The daemon tries its best to make sure all iptables
#   rules have been removed upon shutdown, but the currently enabled rules can be checked with
#   "sudo iptables -S".
#
# InternalCluster::join - this will join back two sub-clusters which must be part of the same rethinkdb
#   cluster.  If there is a value conflict, this script will not handle it very well at the moment, but
#   that functionality can be added.
#
# Cleanup has a few bugs in it, you may need to kill the rethinkdb instances manually, and make sure to
#   remove your folders from /tmp/rethinkdb-port-XXXXXX, which contains the database files for the
#   servers.  The folder name corresponds to the cluster port used by the rethinkdb instance.
#
# There are other objects used for tracking metadata, which provide very little functionality, the exception
#   is the Server, which is specialized into InternalServer and ExternalServer (consistent with the naming
#   for Clusters).  InternalServers are in charge of actually launching and stopping rethinkdb, and are supposed
#   to take care of any cleanup once the server goes out of scope.
#
# Things currently not very well supported
#  - Blueprints - proposals and checking of blueprint data is not implemented
#  - Renaming things - Machines, Datacenters, etc, may be renamed through the cluster, but not yet by this script
#  - Value conflicts - if a value conflict arises due to a split cluster (or some other method), most operations
#     will fail until the conflict is resolved
#

import os
import re
import json
import copy
import time
import socket
import random
import signal
import subprocess
from shutil import rmtree
from httplib import HTTPConnection

def block_path(source_port, dest_port):
    assert "resunder" in subprocess.check_output(["ps", "-A"])
    conn = socket.create_connection(("localhost", 46594))
    conn.sendall("block %s %s\n" % (str(source_port), str(dest_port)))
    # TODO: Wait for ack?
    conn.close()

def unblock_path(source_port, dest_port):
    assert "resunder" in subprocess.check_output(["ps", "-A"])
    conn = socket.create_connection(("localhost", 46594))
    conn.sendall("unblock %s %s\n" % (str(source_port), str(dest_port)))
    conn.close()

def find_rethinkdb_executable(mode = "debug"):
    subpath = "build/%s/rethinkdb" % (mode)
    paths = [subpath, "../" + subpath, "../../" + subpath, "../../../" + subpath]
    for path in paths:
        if os.path.exists(path):
            return path
    raise RuntimeError("Can't find RethinkDB executable. Tried these paths: %s" % paths)

def validate_uuid(json_uuid):
    assert isinstance(json_uuid, str) or isinstance(json_uuid, unicode)
    assert json_uuid.count("-") == 4
    assert len(json_uuid) == 36
    return json_uuid

def is_uuid(json_uuid):
    try:
        validate_uuid(json_uuid)
        return True
    except AssertionError:
        return False

class InvalidServerError(StandardError):
    def __str__(self):
        return "No information about this server is available, server was probably added to the cluster elsewhere"

class ServerExistsError(StandardError):
    def __str__(self):
        return "Attempt to add a server to a cluster where the uuid already exists"

class BadClusterData(StandardError):
    def __init__(self, expected, actual):
        self.expected = expected
        self.actual = actual
    def __str__(self):
        return "Cluster is inconsistent between nodes\nexpected: " + str(self.expected) + "\nactual: " + str(self.actual)

class BadServerResponse(StandardError):
    def __init__(self, status, reason):
        self.status = status
        self.reason = reason
    def __str__(self):
        return "Server returned error code: %d %s" % (self.status, self.reason)

class ValueConflict(object):
    def __init__(self, target, field, resolve_data):
        self.target = target
        self.field = field
        self.values = [ ]
        for value_data in resolve_data:
            self.values.append(value_data[1])
        print self.values

    def __str__(self):
        values = ""
        for value in self.values:
            values += ", \"%s\"" % value
        return "Value conflict on field %s with possible values%s" % (self.field, values)

class Datacenter(object):
    def __init__(self, uuid, json_data):
        self.uuid = uuid
        self.name = json_data[u"name"]

    def check(self, data):
        return data[u"name"] == self.name

    def to_json(self):
        return { u"name": self.name }

    def __str__(self):
        return "Datacenter(name:%s)" % (self.name)

class Blueprint(object):
    def __init__(self, json_data):
        self.peers_roles = json_data[u"peers_roles"]

    def to_json(self):
        return { u"peers_roles": self.peers_roles }

    def __str__(self):
        return "Blueprint()"

class Namespace(object):
    def __init__(self, uuid, json_data):
        self.uuid = validate_uuid(uuid)
        self.blueprint = Blueprint(json_data[u"blueprint"])
        self.primary_uuid = None if json_data[u"primary_uuid"] is None else validate_uuid(json_data[u"primary_uuid"])
        self.replica_affinities = json_data[u"replica_affinities"]
        self.shards = self.parse_shards(json_data[u"shards"])
        self.name = json_data[u"name"]
        self.port = json_data[u"port"]
        self.primary_pinnings = json_data[u"primary_pinnings"]
        self.secondary_pinnings = json_data[u"secondary_pinnings"]

    def check(self, data):
        return data[u"name"] == self.name and data[u"primary_uuid"] == self.primary_uuid and data[u"replica_affinities"] == self.replica_affinities and self.parse_shards(data[u"shards"]) == self.shards and data[u"port"] == self.port

    def to_json(self):
        return {
            unicode("blueprint"): self.blueprint.to_json(),
            unicode("name"): self.name,
            unicode("primary_uuid"): self.primary_uuid,
            unicode("replica_affinities"): self.replica_affinities,
            unicode("shards"): self.shards_to_json(),
            unicode("port"): self.port,
            unicode("primary_pinnings"): self.primary_pinnings,
            unicode("secondary_pinnings"): self.secondary_pinnings
            }

    def __str__(self):
        affinities = ""
        if len(self.replica_affinities) == 0:
            affinities = "None, "
        else:
            for uuid, count in self.replica_affinities.iteritems():
                affinities += uuid + "=" + str(count) + ", "
        if len(self.replica_affinities) == 0:
            shards = "None, "
        else:
            for uuid, count in self.replica_affinities.iteritems():
                shards += uuid + "=" + str(count) + ", "
        return "Namespace(name:%s, port:%d, primary:%s, affinities:%sprimary pinnings:%s, secondary_pinnings:%s, shard boundaries:%s, blueprint:NYI)" % (self.name, self.port, self.primary_uuid, affinities, self.primary_pinnings, self.secondary_pinnings, self.shards)

class DummyNamespace(Namespace):
    def __init__(self, uuid, json_data):
        Namespace.__init__(self, uuid, json_data)

    def shards_to_json(self):
        return self.shards

    def parse_shards(self, subset_strings):
        subsets = [ ]
        superset = set()
        for subset_string in subset_strings:
            subset = u""
            for c in subset_string:
                if c in u"{}, ":
                    pass
                elif c in u"abcdefghijklmnopqrstuvwxyz":
                    assert c not in superset
                    superset.add(c)
                    subset += c
                else:
                    raise RuntimeError("Invalid value in DummyNamespace shard set")
            assert len(subset) != 0
            subsets.append(subset)
        return subsets

    def add_shard(self, new_subset):
        # The shard is a string of characters, a-z, which must be unique across all shards
        if isinstance(new_subset, str):
            new_subset = unicode(new_subset)
        assert isinstance(new_subset, unicode)
        for i in range(len(self.shards)):
            for c in new_subset:
                if c in self.shards[i]:
                    self.shards[i]= self.shards[i].replace(c, u"")
        self.shards.append(new_subset)

    def remove_shard(self, subset):
        if isinstance(subset, str):
            subset = unicode(subset)
        assert isinstance(subset, unicode)
        assert subset in self.shards
        assert len(self.shards) > 0
        self.shards.remove(subset)
        self.shards[0] += subset # Throw the old shard into the first one

    def __str__(self):
        return "Dummy" + Namespace.__str__(self)

class MemcachedNamespace(Namespace):
    def __init__(self, uuid, json_data):
        Namespace.__init__(self, uuid, json_data)

    def shards_to_json(self):
        # Build the ridiculously formatted shard data
        shard_json = []
        last_split = u""
        for split in self.shards:
            shard_json.append(u"[\"%s\", \"%s\"]" % (last_split, split))
            last_split = split
        shard_json.append(u"[\"%s\", null]" % (last_split))
        return shard_json

    def parse_shards(self, shards):
        # Build the ridiculously formatted shard data
        splits = [ ]
        last_split = u""
        matches = None
        for shard in shards:
            matches = re.match(u"^\[\"(\w*)\", \"(\w*)\"\]$|^\[\"(\w*)\", null\]$", shard)
            assert matches is not None
            if matches.group(3) is None:
                assert matches.group(1) == last_split
                splits.append(matches.group(2))
                last_split = matches.group(2)
            else:
                assert matches.group(3) == last_split
        if matches is not None:
            assert matches.group(3) is not None
        assert sorted(splits) == splits
        return splits

    def add_shard(self, split_point):
        if isinstance(split_point, str):
            split_point = unicode(split_point)
        assert split_point not in self.shards
        self.shards.append(split_point)
        self.shards.sort()

    def remove_shard(self, split_point):
        if isinstance(split_point, str):
            split_point = unicode(split_point)
        assert split_point in self.shards
        self.shards.remove(split_point)

    def __str__(self):
        return "Memcached" + Namespace.__str__(self)

class Server(object):
    def __init__(self, serv_host, serv_port):
        self.host = serv_host
        self.cluster_port = serv_port
        self.http_port = serv_port + 1000
        self.uuid = validate_uuid(self.do_query("GET", "/ajax/me"))
        serv_info = self.do_query("GET", "/ajax/machines/" + self.uuid)
        self.datacenter_uuid = serv_info[u"datacenter_uuid"]
        self.name = serv_info[u"name"]
        self.port_offset = serv_info[u"port_offset"]

    def check(self, data):
        # Do not check DummyServer objects
        if isinstance(self, DummyServer):
            return True
        return data[u"datacenter_uuid"] == self.datacenter_uuid and data[u"name"] == self.name and data[u"port_offset"] == self.port_offset

    def to_json(self):
        return { u"datacenter_uuid": self.datacenter_uuid, u"name": self.name, u"port_offset": self.port_offset }

    def do_query(self, method, route, payload = None):
        conn = HTTPConnection(self.host, self.http_port)
        conn.connect()
        if payload is not None:
            conn.request(method, route, json.dumps(payload))
        else:
            conn.request(method, route)
        response = conn.getresponse()
        if response.status == 200:
            return json.loads(response.read())
        else:
            raise BadServerResponse(response.status, response.reason)

    def __str__(self):
        return "Server(%s:%s, name:%s, datacenter:%s, port_offset:%d)" % (self.host, self.cluster_port, self.name, self.datacenter_uuid, self.port_offset)

class DummyServer(Server):
    def __init__(self, uuid, dummy_data):
        self.uuid = uuid

    def do_query(self, method, route, payload = None):
        raise InvalidServer()

    def __str__(self):
        return "DummyServer()"

class ExternalServer(Server):
    def __init__(self, serv_host, serv_port):
        Server.__init__(self, serv_host, serv_port)

    def __str__(self):
        return "External" + Server.__str__(self)

class InternalServer(Server):
    def __init__(self, serv_port, local_cluster_port, join = None, name = None, port_offset = 0, log_file = "stdout", mode = "debug"):

        self.local_cluster_port = local_cluster_port

        # Make a temporary file for the database
        #TODO use a portable method to select db_dir
        self.db_dir = "/tmp/rethinkdb-port-" + str(serv_port)
        assert not os.path.exists(self.db_dir)

        create_args = [find_rethinkdb_executable(mode), "create", "--directory=" + self.db_dir, "--port-offset=" + str(port_offset)]
        if name is not None:
            create_args.append("--name=" + name)

        if log_file == "stdout":
            self.output_file = None
        else:
            self.log_file = log_file
            self.output_file = open(self.log_file, "w")

        subprocess.check_call(args = create_args, stdout = self.output_file, stderr = self.output_file)
        self.args_without_join = [find_rethinkdb_executable(mode), "serve", "--directory=" + self.db_dir, "--port=" + str(serv_port), "--client-port=" + str(local_cluster_port)]

        if join is None:
            serve_args = self.args_without_join
        else:
            join_host, join_port = join
            serve_args = self.args_without_join + ["--join=" + join_host + ":" + str(join_port)]

        print serve_args

        self.instance = subprocess.Popen(args = serve_args, stdout = self.output_file, stderr = self.output_file)
        time.sleep(0.2)
        Server.__init__(self, socket.gethostname(), serv_port)
        self.running = True

    def kill(self):
        assert self.running
        try:
            self.instance.send_signal(signal.SIGINT)
        except OSError:
            pass
        self._wait()
        self.running = False

    def recover(self, join = None):
        assert not self.running
        if join is None:
            serve_args = self.args_without_join
        else:
            join_host, join_port = join
            serve_args = self.args_without_join + ["--join=" + join_host + ":" + str(join_port)]

        self.instance = subprocess.Popen(args = serve_args, stdout = self.output_file)
        self.running = True

    def shutdown(self):
        if self.running:
            self.kill()
        rmtree(self.db_dir, True)

    def __del__(self):
        self.shutdown()

    def _wait(self):
        start_time = time.time()
        while time.time() - start_time < 15 and self.instance.poll() is None:
            time.sleep(1)
        if self.instance.poll() is None:
            print "rethinkdb unresponsive to SIGINT after 15 seconds, using SIGKILL"
            self.instance.send_signal(signal.SIGKILL)

    def __str__(self):
        return "Internal" + Server.__str__(self) + ", args:" + str(self.args_without_join)

class Cluster(object):
    def __init__(self, log_file = "stdout", mode = "debug"):
        try:
            self.base_port = int(os.environ["RETHINKDB_BASE_PORT"])
        except KeyError:
            self.base_port = random.randint(20000, 60000)
            print "Warning: environment variable 'RETHINKDB_BASE_PORT' not set, using random base port: " + str(self.base_port)

        self.server_instances = 0
        self.machines = { }
        self.datacenters = { }
        self.dummy_namespaces = { }
        self.memcached_namespaces = { }
        self.log_file = log_file
        self.mode = mode
        self.conflicts = [ ]

    def __str__(self):
        retval = "Machines:"
        for i in self.machines.iterkeys():
            retval += "\n%s: %s" % (i, self.machines[i])
        retval += "\nDatacenters:"
        for i in self.datacenters.iterkeys():
            retval += "\n%s: %s" % (i, self.datacenters[i])
        retval += "\nNamespaces:"
        for i in self.dummy_namespaces.iterkeys():
            retval += "\n%s: %s" % (i, self.dummy_namespaces[i])
        for i in self.memcached_namespaces.iterkeys():
            retval += "\n%s: %s" % (i, self.memcached_namespaces[i])
        return retval

    def print_machines(self):
        for i in self.machines.iterkeys():
            print "%s: %s" % (i, self.machines[i])

    def print_namespaces(self):
        for i in self.dummy_namespaces.iterkeys():
            print "%s: %s" % (i, self.dummy_namespaces[i])
        for i in self.memcached_namespaces.iterkeys():
            print "%s: %s" % (i, self.memcached_namespaces[i])

    def print_datacenters(self):
        for i in self.datacenters.iterkeys():
            print "%s: %s" % (i, self.datacenters[i])

    def _get_server_for_command(self, servid = None):
        if servid is None:
            for serv in self.machines.itervalues():
                if not isinstance(serv, DummyServer):
                    return serv
        else:
            return self.machines[servid]

    # Add a machine to the cluster by starting a server instance locally
    def add_machine(self, name = None):
        if self.server_instances == 0:
            # First server in cluster shouldn't connect to anyone
            join = None
        else:
            join = (socket.gethostname(), self.base_port)

        log_file = self.log_file
        if self.log_file != "stdout":
            log_file += ".%d" % (self.base_port + self.server_instances)

        serv = InternalServer(
            self.base_port + self.server_instances,
            self.base_port - self.server_instances - 1,
            join = join,
            name = name,
            port_offset = self.server_instances,
            log_file = log_file,
            mode = self.mode)
        self.machines[serv.uuid] = serv
        self.server_instances += 1
        time.sleep(0.2)
        self.update_cluster_data()
        return serv

    # Add a machine that was added elsewhere - there should already be a dummy server instance as a placeholder
    def add_existing_machine(self, serv):
        assert isinstance(serv, Server)
        old = self.machines.get(serv.uuid)
        if old is not None:
            # If the old uuid is a dummy server, replace it
            if not isinstance(old, DummyServer):
                raise ServerExistsError()
        self.machines[serv.uuid] = serv
        self.server_instances += 1
        self.update_cluster_data()
        return serv

    def add_datacenter(self, name = None, servid = None):
        if name is None:
            name = str(random.randint(0, 1000000))
        info = self._get_server_for_command(servid).do_query("POST", "/ajax/datacenters/new", {
            "name": name
            })
        time.sleep(0.2) # Give some time for changes to hit the rest of the cluster
        assert len(info) == 1
        uuid, json_data = next(info.iteritems())
        datacenter = Datacenter(uuid, json_data)
        self.datacenters[datacenter.uuid] = datacenter
        self.update_cluster_data()
        return datacenter

    def _find_thing(self, what, type_class, type_str, search_space):
        if isinstance(what, (str, unicode)):
            if is_uuid(what):
                return search_space[what]
            else:
                hits = [x for x in search_space.values() if x.name == what]
                if len(hits) == 0:
                    raise ValueError("No %s named %r" % (type_str, what))
                elif len(hits) == 1:
                    return hits[0]
                else:
                    raise ValueError("Multiple %ss named %r" % (type_str, what))
        elif isinstance(what, type_class):
            assert search_space[what.uuid] is what
            return what
        else:
            raise TypeError("Can't interpret %r as a %s" % (what, type_str))

    def find_machine(self, what):
        return self._find_thing(what, Server, "machine", self.machines)

    def find_datacenter(self, what):
        return self._find_thing(what, Datacenter, "data center", self.datacenters)

    def find_namespace(self, what):
        nss = {}
        nss.update(self.memcached_namespaces)
        nss.update(self.dummy_namespaces)
        return self._find_thing(what, Namespace, "namespace", nss)

    def move_server_to_datacenter(self, serv, datacenter, servid = None):
        serv = self.find_machine(serv)
        datacenter = self.find_datacenter(datacenter)
        if not isinstance(serv, DummyServer):
            serv.datacenter_uuid = datacenter.uuid
        self._get_server_for_command(servid).do_query("POST", "/ajax/machines/" + serv.uuid + "/datacenter_uuid", datacenter.uuid)
        time.sleep(0.2) # Give some time for changes to hit the rest of the cluster
        self.update_cluster_data()

    def move_namespace_to_datacenter(self, namespace, primary, servid = None):
        namespace = self.find_namespace(namespace)
        primary = None if primary is None else self.find_datacenter(primary)
        namespace.primary_uuid = primary.uuid
        if isinstance(namespace, MemcachedNamespace):
            self._get_server_for_command(servid).do_query("POST", "/ajax/memcached_namespaces/" + namespace.uuid, namespace.to_json())
        elif isinstance(namespace, DummyNamespace):
            self._get_server_for_command(servid).do_query("POST", "/ajax/dummy_namespaces/" + namespace.uuid, namespace.to_json())
        time.sleep(0.2) # Give some time for the changes to hit the rest of the cluster
        self.update_cluster_data()

    def set_namespace_affinities(self, namespace, affinities = { }, servid = None):
        namespace = self.find_namespace(namespace)
        aff_dict = { }
        for datacenter, count in affinities.iteritems():
            aff_dict[self.find_datacenter(datacenter).uuid] = count
        namespace.replica_affinities = aff_dict
        if isinstance(namespace, MemcachedNamespace):
            self._get_server_for_command(servid).do_query("POST", "/ajax/memcached_namespaces/" + namespace.uuid, namespace.to_json())
        elif isinstance(namespace, DummyNamespace):
            self._get_server_for_command(servid).do_query("POST", "/ajax/dummy_namespaces/" + namespace.uuid, namespace.to_json())
        time.sleep(0.2) # Give some time for the changes to hit the rest of the cluster
        self.update_cluster_data()
        return namespace

    def add_namespace(self, protocol = "memcached", name = None, port = None, primary = None, affinities = { }, servid = None):
        if port is None:
            port = random.randint(20000, 60000)
        if name is None:
            name = str(random.randint(0, 1000000))
        if primary is not None:
            primary = self.find_datacenter(primary).uuid
        else:
            primary = random.choice(self.datacenters.keys())
        aff_dict = { }
        for datacenter, count in affinities.iteritems():
            aff_dict[self.find_datacenter(datacenter).uuid] = count
        info = self._get_server_for_command(servid).do_query("POST", "/ajax/%s_namespaces/new" % protocol, {
            "name": name,
            "port": port,
            "primary_uuid": primary,
            "replica_affinities": aff_dict
            })
        time.sleep(0.2) # Give some time for changes to hit the rest of the cluster
        assert len(info) == 1
        uuid, json_data = next(info.iteritems())
        type_class = {"memcached": MemcachedNamespace, "dummy": DummyNamespace}[protocol]
        namespace = type_class(uuid, json_data)
        getattr(self, "%s_namespaces" % protocol)[namespace.uuid] = namespace
        self.update_cluster_data()
        return namespace

    def rename(self, target, name, servid = None):
        type_targets = { MemcachedNamespace: self.memcached_namespaces, DummyNamespace: self.dummy_namespaces, InternalServer: self.machines, ExternalServer: self.machines, DummyServer: self.machines, Datacenter: self.datacenters }
        type_objects = { MemcachedNamespace: "memcached_namespaces", DummyNamespace: "dummy_namespaces", InternalServer: "machines", ExternalServer: "machines", DummyServer: "machines", Datacenter: "datacenters" }
        assert type_targets[type(target)][target.uuid] is target
        object_type = type_objects[type(target)]
        target.name = name
        info = self._get_server_for_command(servid).do_query("POST", "/ajax/%s/%s/name" % (object_type, target.uuid), name)
        time.sleep(0.2)
        self.update_cluster_data()

    def get_conflicts(self):
        return self.conflicts

    def resolve_conflict(self, conflict, value, servid = None):
        assert conflict in self.conflicts
        assert value in conflict.values
        type_targets = { MemcachedNamespace: self.memcached_namespaces, DummyNamespace: self.dummy_namespaces, InternalServer: self.machines, ExternalServer: self.machines, DummyServer: self.machines, Datacenter: self.datacenters }
        type_objects = { MemcachedNamespace: "memcached_namespaces", DummyNamespace: "dummy_namespaces", InternalServer: "machines", ExternalServer: "machines", DummyServer: "machines", Datacenter: "datacenters" }
        assert type_targets[type(conflict.target)][conflict.target.uuid] is conflict.target
        object_type = type_objects[type(conflict.target)]
        info = self._get_server_for_command(servid).do_query("POST", "/ajax/%s/%s/%s/resolve" % (object_type, conflict.target.uuid, conflict.field), value)
        # Remove the conflict and update the field in the target 
        self.conflicts.remove(conflict)
        setattr(conflict.target, conflict.field, value) # TODO: this probably won't work for certain things like shards that we represent differently locally than the strict json format
        time.sleep(0.2)
        self.update_cluster_data()

    def add_namespace_shard(self, namespace, split_point, servid = None):
        type_namespaces = { MemcachedNamespace: self.memcached_namespaces, DummyNamespace: self.dummy_namespaces }
        type_protocols = { MemcachedNamespace: "memcached", DummyNamespace: "dummy" }
        assert type_namespaces[type(namespace)][namespace.uuid] is namespace
        protocol = type_protocols[type(namespace)]
        namespace.add_shard(split_point)
        info = self._get_server_for_command(servid).do_query("POST", "/ajax/%s_namespaces/%s/shards" % (protocol, namespace.uuid), namespace.shards_to_json())
        time.sleep(0.2)
        self.update_cluster_data()

    def remove_namespace_shard(self, namespace, split_point, servid = None):
        type_namespaces = { MemcachedNamespace: self.memcached_namespaces, DummyNamespace: self.dummy_namespaces }
        type_protocols = { MemcachedNamespace: "memcached", DummyNamespace: "dummy" }
        assert type_namespaces[type(namespace)][namespace.uuid] is namespace
        protocol = type_protocols[type(namespace)]
        namespace.remove_shard(split_point)
        info = self._get_server_for_command(servid).do_query("POST", "/ajax/%s_namespaces/%s/shards" % (protocol, namespace.uuid), namespace.shards_to_json())
        time.sleep(0.2)
        self.update_cluster_data()

    def compute_port(self, namespace, machine):
        namespace = self.find_namespace(namespace)
        machine = self.find_machine(machine)
        return namespace.port + machine.port_offset

    def get_namespace_host(self, namespace, selector = None):
        # selector may be a specific machine or datacenter to use, none will take any
        type_namespaces = { MemcachedNamespace: self.memcached_namespaces, DummyNamespace: self.dummy_namespaces }
        assert type_namespaces[type(namespace)][namespace.uuid] is namespace
        if selector is None:
            # Take any machine (make sure it isn't a DummyServer)
            machines = [ ]
            for serv in self.machines.itervalues():
                if not isinstance(serv, DummyServer):
                    machines.append(serv)
            machine = random.choice(machines)
        elif isinstance(selector, Datacenter):
            # Take any machine from the specified datacenter
            machine = self.get_machine_in_datacenter(selector)
        elif isinstance(selector, Server):
            # Use the given server directly
            machine = selector

        return (machine.host, self.compute_port(namespace, machine))

    def get_datacenter_in_namespace(self, namespace, primary = None):
        type_namespaces = { MemcachedNamespace: self.memcached_namespaces, DummyNamespace: self.dummy_namespaces }
        assert type_namespaces[type(namespace)][namespace.uuid] is namespace
        if primary is not None:
            return self.datacenters[namespace.primary_uuid]

        # Build a list of datacenters in the given namespace
        datacenters = [ self.datacenters[namespace.primary_uuid] ]
        for uuid in namespace.replica_affinities.iterkeys():
            datacenters.append(self.datacenters[uuid])
        return random.choice(datacenters)

    def get_machine_in_datacenter(self, datacenter):
        assert self.datacenters[datacenter.uuid] is datacenter
        # Build a list of machines in the given datacenter
        machines = [ ]
        for serv in self.machines.itervalues():
            if serv.datacenter_uuid == datacenter.uuid and not isinstance(serv, DummyServer):
                machines.append(serv)
        return random.choice(machines)

    def _pull_cluster_data(self, cluster_data, local_data, data_type):
        for uuid in cluster_data.iterkeys():
            validate_uuid(uuid)
            if uuid not in local_data:
                local_data[uuid] = data_type(uuid, cluster_data[uuid])
        assert len(cluster_data) == len(local_data)

    # Get the list of machines/namespaces from the cluster, verify that it is consistent across each machine
    def _verify_consistent_cluster(self):
        expected = self._get_server_for_command().do_query("GET", "/ajax")
        # Filter out the "me" value - it will be different on each machine
        assert expected.pop("me") is not None
        for i in self.machines.iterkeys():
            if isinstance(self.machines[i], DummyServer): # Don't try to query a server we don't know anything about
                continue

            actual = self.machines[i].do_query("GET", "/ajax")
            assert actual.pop("me") == self.machines[i].uuid
            if actual != expected:
                raise BadClusterData(expected, actual)
        return expected

    def _verify_cluster_data_chunk(self, local, remote):
        for uuid, obj in local.iteritems():
            check_obj = True
            for field, value in remote[uuid].iteritems():
                if value == u"VALUE_IN_CONFLICT":
                    if obj not in self.conflicts:
                        # Get the possible values and create a value conflict object
                        type_objects = { MemcachedNamespace: "memcached_namespaces", DummyNamespace: "dummy_namespaces", InternalServer: "machines", ExternalServer: "machines", DummyServer: "machines", Datacenter: "datacenters" }
                        object_type = type_objects[type(obj)]
                        resolve_data = self._get_server_for_command().do_query("GET", "/ajax/%s/%s/%s/resolve" % (object_type, obj.uuid, field))
                        self.conflicts.append(ValueConflict(obj, field, resolve_data))
                    print "Warning: value conflict"
                    check_obj = False
                    
            if check_obj and not obj.check(remote[uuid]):
                raise ValueError("inconsistent cluster data: %r != %r" % (obj.to_json(), remote[uuid]))

    # Check the data from the server against our data
    def _verify_cluster_data(self, data):
        self._verify_cluster_data_chunk(self.machines, data[u"machines"])
        self._verify_cluster_data_chunk(self.datacenters, data[u"datacenters"])
        self._verify_cluster_data_chunk(self.dummy_namespaces, data[u"dummy_namespaces"])
        self._verify_cluster_data_chunk(self.memcached_namespaces, data[u"memcached_namespaces"])

    def update_cluster_data(self):
        data = self._verify_consistent_cluster()
        self._pull_cluster_data(data[u"machines"], self.machines, DummyServer)
        self._pull_cluster_data(data[u"datacenters"], self.datacenters, Datacenter)
        self._pull_cluster_data(data[u"dummy_namespaces"], self.dummy_namespaces, DummyNamespace)
        self._pull_cluster_data(data[u"memcached_namespaces"], self.memcached_namespaces, MemcachedNamespace)
        self._verify_cluster_data(data)
        return data

    def is_alive(self):
        for i, m in self.machines.iteritems():
            if isinstance(m, InternalServer):
                if m.running and m.instance.poll() is not None:
                    return False
        return True

class ExternalCluster(Cluster):
    def __init__(self, serv_list):
        Cluster.__init__(self)
        # Save servers in the cluster by uuid
        for serv in serv_list:
            self.machines[serv.uuid] = serv
            self.server_instances += 1

        # Pull any existing cluster information
        self.update_cluster_data()

class InternalCluster(Cluster):
    # datacenters - array of counts - number of machines to put in each datacenter
    # affinities - array of arrays of tuples - first array - one item per namespace type (dummy and memcached in that order)
    #                                          second array - one item per namespace to create
    #                                          tuple - first value is the index of the primary datacenter, second value is
    #                                              an array with one item per datacenter, an integer value for the affinity towards that datacenter
    def __init__(self, datacenters = [ ], affinities = [ ], log_file = "stdout", mode = "debug"):
        assert len(affinities) <= 2 # only two namespace types at the moment - dummy and memcached
        for i in affinities:
            for primary_id, replica_counts in i:
                assert primary_id < len(datacenters) # make sure the primary datacenter id doesn't overflow
                assert len(replica_counts) == len(datacenters) or len(replica_counts) == 0 # namespace affinities must define values for each datacenter or no affinities

        Cluster.__init__(self, log_file, mode)
        self.blocked_ports = set()
        self.other_clusters = set()

        while len(self.machines) < sum(datacenters):
            self.add_machine()
        assert len(self.machines) == sum(datacenters)

        while len(self.datacenters) < len(datacenters):
            self.add_datacenter()
        assert len(self.datacenters) == len(datacenters)

        # Balance servers across datacenters as requested
        server_iter = self.machines.itervalues()
        datacenter_list = self.datacenters.values()
        assert len(self.machines) == sum(datacenters)
        for i in range(len(datacenters)):
            for j in range(datacenters[i]):
                self.move_server_to_datacenter(next(server_iter), datacenter_list[i])

        datacenter_list = self.datacenters.values()
        # Initialize dummy namespaces with given affinities
        if len(affinities) >= 1:
            affinity_offset = 0
            for namespace in self.dummy_namespaces.itervalues():
                self._initialize_namespace(namespace, datacenter_list, affinities[0][affinity_offset])
                affinity_offset += 1
            while len(self.dummy_namespaces) < len(affinities[0]):
                self._initialize_namespace(self.add_namespace("dummy"), datacenter_list, affinities[0][affinity_offset])
                affinity_offset += 1
            assert affinity_offset == len(affinities[0])

        # Initialize memcached namespaces with given affinities
        if len(affinities) >= 2:
            affinity_offset = 0
            for namespace in self.memcached_namespaces.itervalues():
                self._initialize_namespace(namespace, datacenter_list, affinities[1][affinity_offset])
                affinity_offset += 1
            while len(self.memcached_namespaces) < len(affinities[1]):
                self._initialize_namespace(self.add_namespace("memcached"), datacenter_list, affinities[1][affinity_offset])
                affinity_offset += 1
            assert affinity_offset == len(affinities[1])

        self.closed = False

    def shutdown(self):
        assert not self.closed
        # Clean up any remaining blocked paths
        for m in self.machines.itervalues():
            for dest_port in self.blocked_ports:
                unblock_path(m.local_cluster_port, dest_port)
            m.shutdown()
        self.closed = True

    def __del__(self):
        if not self.closed:
            self.shutdown()

    def _initialize_namespace(self, namespace, datacenter_list, affinity):
        primary, aff_data = affinity
        if len(aff_data) > 0:
            a = { }
            for d in range(len(datacenter_list)):
                a[datacenter_list[d]] = aff_data[d]
            self.set_namespace_affinities(namespace, a)
        self.move_namespace_to_datacenter(namespace, datacenter_list[primary])

    # Sets up iptables rules to isolate a set of machines from the cluster, constructs a new cluster object with the selected machines
    # This won't block any DummyServers in your cluster (ExternalServers that have not been initialized by the user)
    def split(self, machines):
        if isinstance(machines, int):
            # Pick n arbitrary machines
            n = machines
            machines = [ ]
            machines_list = self.machines.values()
            machines_index = 0
            for i in range(n):
                while isinstance(machines_list[machines_index], DummyServer):
                    machines_index += 1
                machines.append(machines_list[machines_index])
                machines_index += 1

        for m in machines:
            assert m.uuid in self.machines
            assert not isinstance(m, DummyServer)

        # Create a new cluster and copy all cluster data
        new_cluster = InternalCluster([ ], [ ])
        new_cluster.blocked_ports = copy.deepcopy(self.blocked_ports)
        new_cluster.datacenters = copy.deepcopy(self.datacenters)
        new_cluster.dummy_namespaces = copy.deepcopy(self.dummy_namespaces)
        new_cluster.memcached_namespaces = copy.deepcopy(self.memcached_namespaces)

        ports_to_block_self = set()
        ports_to_block_new = set()

        # Move selected machines into the new cluster
        for m in machines:
            new_cluster.machines[m.uuid] = m
            self.machines.pop(m.uuid)
            ports_to_block_self.add(m.cluster_port)

        for m in self.machines.values():
            if not isinstance(m, DummyServer):
                ports_to_block_new.add(m.cluster_port)

        # Block ports from this cluster to the new cluster
        for m in self.machines.values():
            if not isinstance(m, DummyServer):
                for p in ports_to_block_self:
                    block_path(m.local_cluster_port, p)

        # Block ports from the new cluster to this cluster
        for m in new_cluster.machines.values():
            if not isinstance(m, DummyServer):
                for p in ports_to_block_new:
                    block_path(m.local_cluster_port, p)

        self.blocked_ports = self.blocked_ports | ports_to_block_self
        new_cluster.blocked_ports = new_cluster.blocked_ports | ports_to_block_new

        # Make sure all other clusters know about the new cluster
        for c in self.other_clusters:
            c.other_clusters.add(new_cluster)

        new_cluster.other_clusters = copy.copy(self.other_clusters)
        new_cluster.other_clusters.add(self)
        self.other_clusters.add(new_cluster)

        # This should fill in missing machines with DummyServers
        self.update_cluster_data()
        new_cluster.update_cluster_data()

        return new_cluster

    # Removes the iptables blocked ports to join this cluster with the cluster passed as an argument, which is then deleted
    # These two clusters must be pieces of the same original cluster
    def join(self, other):
        assert other in self.other_clusters
        assert self in other.other_clusters

        # Remove blocks between the clusters
        for m in self.machines.values():
            if not isinstance(m, DummyServer):
                for n in other.machines.values():
                    if not isinstance(n, DummyServer):
                        unblock_path(m.local_cluster_port, n.cluster_port)
                        unblock_path(n.local_cluster_port, m.cluster_port)

        for m in self.machines.values():
            if not isinstance(m, DummyServer):
                other.blocked_ports.remove(m.cluster_port)
        for m in other.machines.values():
            if not isinstance(m, DummyServer):
                self.blocked_ports.remove(m.cluster_port)
                self.machines[m.uuid] = m

        # Add items from other cluster into this cluster
        self.datacenters.update(other.datacenters)
        self.dummy_namespaces.update(other.dummy_namespaces)
        self.memcached_namespaces.update(other.memcached_namespaces)

        # Update other_clusters in this cluster
        self.other_clusters.remove(other)
        other.other_clusters.remove(self)

        # Update other_clusters in all remaining clusters
        for c in self.other_clusters:
            c.other_clusters.remove(other)

        # Do some sanity checks to make sure everything is working
        assert self.blocked_ports == other.blocked_ports
        assert self.other_clusters == other.other_clusters
        # Clear out other cluster as it is no longer valid
        other.machines = { }
        other.datacenters = { }
        other.dummy_namespaces = { }
        other.memcached_namespaces = { }
        other.blocked_ports = set()
        other.other_cluster = set()

        # Give some time for the cluster to update internally, then pull the cluster data
        time.sleep(5)
        self.update_cluster_data()

    def _notify_of_new_cluster_port(self, port):
        self.blocked_ports.add(port)
        for m in self.machines.itervalues():
            if not isinstance(m, DummyServer):
                block_path(m.cluster_port, port)

    def add_machine(self, name = None):
        # Block the new machine's port across all pieces of the cluster before we actually run the server
        new_local_cluster_port = self.base_port - self.server_instances - 1
        new_cluster_port = self.base_port + self.server_instances
        for c in self.other_clusters:
            c._notify_of_new_cluster_port(new_cluster_port)
        for p in self.blocked_ports:
            block_path(new_local_cluster_port, p)
        return Cluster.add_machine(self, name)

    def add_existing_machine(self, machine):
        assert len(self.other_clusters) != 0 # Cannot have a split cluster with externally-added machines
        return Cluster.add_existing_machine(self)

    # Kills the rethinkdb process with SIGINT, leaves the Server object so it may be restarted with the same data
    def kill_machines(self, machines):
        for m in machines:
            assert m.uuid in self.machines

        for m in machines:
            m.kill()

    # Brings machines back into the cluster, by restarting the killed process, or unblocking ports
    def recover_machines(self, machines):
        for m in machines:
            assert m.uuid in self.machines

        for m in machines:
            m.recover()
