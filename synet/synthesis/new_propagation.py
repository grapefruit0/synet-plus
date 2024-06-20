# !/usr/bin/env python

"""synthesize configurations for eBGP protocol"""

import copy
import logging

import networkx as nx
import z3

from synet.settings import *
from synet.synthesis.ebgp_verify import EBGPVerify
from synet.synthesis.new_bgp import BGP
from tekton.graph import NetworkGraph
from synet.utils.bgp_utils import PropagatedInfo
from synet.utils.bgp_utils import annotate_graph
from synet.utils.bgp_utils import compute_next_hop_map
from synet.utils.bgp_utils import compute_propagation
from synet.utils.bgp_utils import print_compute_propagation
from synet.utils.common import KConnectedPathsReq
from synet.utils.common import PathOrderReq
from synet.utils.common import PathReq
from synet.utils.common import Req
from synet.utils.common import flatten
from synet.utils.fnfree_smt_context import ASPATH_SORT
from synet.utils.fnfree_smt_context import is_empty
from synet.utils.fnfree_smt_context import is_symbolic
from synet.utils.smt_context import get_as_path_key


__author__ = "Ahmed El-Hassany"     # maintainer Yongzheng Zhang
__email__ = "a.hassany@gmail.com"   # yongzheng2024@outlook.com


class EBGPPropagation(object):
    """compute the BGP route propagation graph"""

    def __init__(self, reqs, network_graph, ctx):
        log_name = '%s.%s' % (self.__module__, self.__class__.__name__)
        self.log = logging.getLogger(log_name)
        assert isinstance(network_graph, NetworkGraph)
        for req in reqs:
            assert isinstance(req, Req)
        self.reqs = reqs
        self.network_graph = network_graph
        self.ctx = ctx
        self.verify = EBGPVerify(self.network_graph, self.reqs)
        self.ebgp_graphs = {}  # eBGP propagation graphs
        self.ibgp_graphs = {}  # iBGP propagation graphs
        self.ebgp_propagation = None
        self.ibgp_propagation = None
        self.ibgp_zones = self.extract_ibgp_zones()
        self.next_hop_map = compute_next_hop_map(self.network_graph)
        self.set_bgp_router_ids()

    def set_bgp_router_ids(self):
        ids = []
        for router in self.network_graph.routers_iter():
            if not self.network_graph.is_bgp_enabled(router):
                continue
            router_id = self.network_graph.get_bgp_router_id(router)
            if not router_id:
                # Sketch doesn't allow setting router ID
                continue
            elif is_empty(router_id):
                # Sketch has the router ID to be symbolic
                router_id = None
            elif hasattr(router_id, 'is_concrete'):
                if router_id.is_concrete:
                    router_id = router_id.get_value()
                else:
                    router_id = None
            var = self.ctx.create_fresh_var(z3.IntSort(self.ctx.z3_ctx),
                                            value=router_id,
                                            name_prefix='{}_router_id'.format(router))
            ids.append(var)
            self.network_graph.set_bgp_router_id(router, var)
        if not ids:
            # No router IDs used in the sketch
            return
        for var in ids:
            self.ctx.register_constraint(var.var > 0, name_prefix='router_id_larger_than_zero_')
        dist = [var.var for var in ids]
        if all([var.is_concrete for var in ids]):
            unq = len(set(dist)) == len(dist)
        else:
            dist += [self.ctx.z3_ctx]
            unq = z3.Distinct(*dist)
        self.ctx.register_constraint(unq == True, name_prefix='router_id_unique')

    def extract_ibgp_zones(self):
        """Extract subgraphs such that each subgraph represents all routers within an AS"""
        asmap = dict()  # Map asnum -> list of routers in that AS
        for node in self.network_graph.routers_iter():
            if self.network_graph.is_bgp_enabled(node):
                asnum = self.network_graph.get_bgp_asnum(node)
                if asnum not in asmap:
                    asmap[asnum] = []
                asmap[asnum].append(node)

        zones = {}
        for asnum, nodes in asmap.iteritems():
            ibgp_nodes = copy.copy(nodes)
            ibgp_graph = nx.Graph()
            for node in ibgp_nodes:
                ibgp_graph.add_node(node)
            prev_size = (ibgp_graph.number_of_nodes(), ibgp_graph.number_of_edges())
            changed = True
            while changed:
                for node in ibgp_nodes:
                    for neighbor in self.network_graph.neighbors(node):
                        if not self.network_graph.is_router(neighbor):
                            continue
                        # ibgp
                        if self.network_graph.is_bgp_enabled(neighbor):
                            if self.network_graph.get_bgp_asnum(neighbor) == asnum:
                                ibgp_graph.add_edge(node, neighbor)
                        # other protocol that established peer (static, ospf)
                        else:
                            ibgp_graph.add_edge(node, neighbor)
                new_size = (ibgp_graph.number_of_nodes(), ibgp_graph.number_of_edges())
                if new_size != prev_size:
                    changed = True
                    prev_size = new_size
                    ibgp_nodes = list(ibgp_graph.nodes())
                else:
                    changed = False
            zones[asnum] = ibgp_graph
        return zones

    def add_path_req(self, req):
        """
        Add new requirement
        :param req: instance of PathReq
        :return: None
        """
        assert isinstance(req, Req)
        self.reqs.append(req)

    def get_bgp_path(self, path):
        """
        Given path of routers, return the AS path in reversed order
        For instance: (R1, R2_0, R2_1, R3) return (300, 200, 100)
        :return tuple of AS numbers
        """
        bgp_path = []
        for node in path:
            if not self.network_graph.is_bgp_enabled(node):
                continue
            asnum = self.network_graph.get_bgp_asnum(node)
            if not bgp_path or (bgp_path and bgp_path[-1] != asnum):
                bgp_path.append(asnum)
        return tuple(reversed(bgp_path))

    def extract_reqs(self, reqs):
        """
        For each requirement return the AS paths and router paths

        path1 = [a, b, c, d], path2 = [e, f, g, h], paths = [path1, path2]
          a.asnum = 100, b.asnum = c.asnum = 200, d.asnum = 300
          e.asnum = 400, f.asnum = g.asnum = 500, h.asnum = 600

        reqs is PathReq (reqs.path is path1)
          as_paths:     [set([(d, (300, 200, 100))])]
          router_paths: [set([(d, (d, c, b, a))])]
        reqs is PathOrderReq or KConnectedPathsReq (reqs.paths is paths)
          as_paths:     [set([(d, (300, 200, 100))]), set([(h, (600, 500, 400))])]
          router_pahts: [set([(d, (d, c, b, a))]), set([(h, (h, g, f, e))])]
        """
        as_paths = []
        router_paths = []
        for req in reqs:
            if isinstance(req, PathReq):
                path = self.get_bgp_path(req.path)
                as_paths.append(set([(req.path[-1], path)]))
                router_paths.append(set([(req.path[-1], tuple(reversed(req.path)))]))
            elif isinstance(req, PathOrderReq):
                bgp, full = self.extract_reqs(req.paths)
                as_paths.extend(bgp)
                router_paths.extend(full)
            elif isinstance(req, KConnectedPathsReq):
                bgp, full = self.extract_reqs(req.paths)
                as_paths.append(set(flatten(bgp)))
                router_paths.append(set(flatten(full)))
            else:
                raise ValueError("Unknown req type %s" % req)
        return as_paths, router_paths

    def expand_as_path(self, path, origins):
        """
        Given an AS Path, expand to include all the routers in the path
        For example (100, 200, 300) ->
            (R1, R2_0, R2_1, R3), (R1, R2_0, R2_3, R3), etc..
        :return list of all paths
        """
        ibgp_entry = {0: origins}
        tt = [set(self.ibgp_zones[asnum].nodes()) for asnum in path]
        new_paths = [[o] for o in ibgp_entry[0]]

        for index, _ in enumerate(tt):
            is_last = index == len(tt) - 1
            for path in new_paths[:]:
                if not is_last:
                    # None terminating path, remove it
                    new_paths.remove(path)
                last_node = path[-1]
                bgp_neighbors = list(self.network_graph.get_bgp_neighbors(last_node).keys())
                curras = self.network_graph.get_bgp_asnum(last_node)
                for neighbor in bgp_neighbors:
                    neighbor_as = self.network_graph.get_bgp_asnum(neighbor)
                    #if is_last and curras == neighbor_as:
                    #    if (len(path) < 3 or self.network_graph.get_bgp_asnum(path[-2]) != curras):
                    #        # Single hop iBGP append
                    #        new_paths.append(path + [neighbor])
                    #        print "SINGLE HOP", new_paths[-1]
                    if not is_last and curras != neighbor_as:
                        if neighbor not in path and neighbor in tt[index + 1]:
                            new_path = path + [neighbor]
                            new_paths.append(new_path)
                            for nn in self.network_graph.get_bgp_neighbors(new_path[-1]):
                                nn_as = self.network_graph.get_bgp_asnum(nn)
                                if curras != neighbor_as and neighbor_as == nn_as:
                                    new_paths.append(new_path + [nn])
        return set([tuple(path) for path in new_paths])

    def expand_ebgp_graph(self, graph, expanded, ebgp_paths, ibgp_paths):
        origins = {}
        for origin, path in flatten(ebgp_paths):
            if path[0] not in origins:
                origins[path[0]] = set()
            origins[path[0]].add(origin)
        all_paths = set()
        expanded_paths = {}
        for node in graph.nodes():
            allowed_paths = graph.node[node]['paths']
            blocked_paths = graph.node[node]['block']
            for path in allowed_paths.union(blocked_paths):
                all_paths.add(path)
        for path in all_paths:
            expanded_paths[path] = self.expand_as_path(path, origins[path[0]])
        for _, paths in expanded_paths.iteritems():
            for path in paths:
                node = path[-1]
                if path in expanded.node[node]['paths']:
                    continue
                else:
                    expanded.node[node]['block'].add(path)

    def merge_dags(self):
        ebgp_propagation = nx.Graph()
        ibgp_propagation = nx.Graph()
        for net, graph in self.ebgp_graphs.iteritems():
            for node in graph.nodes():
                if not ebgp_propagation.has_node(node):
                    ebgp_propagation.add_node(node, nets={})
                info = {
                    'order': graph.node[node]['order'],
                    'paths': graph.node[node]['paths'],
                    'block': graph.node[node]['block'],
                }
                ebgp_propagation.node[node]['nets'][net] = info
            for src, dst in graph.edges():
                ebgp_propagation.add_edge(src, dst)
        for net, graph in self.ibgp_graphs.iteritems():
            for node in graph.nodes():
                if not ibgp_propagation.has_node(node):
                    ibgp_propagation.add_node(node, nets={})
                info = {
                    'order': graph.node[node]['order'],
                    'paths': graph.node[node]['paths'],
                    'block': graph.node[node]['block'],
                }
                ibgp_propagation.node[node]['nets'][net] = info
            for src, dst in graph.edges():
                ibgp_propagation.add_edge(src, dst)
        self.ebgp_propagation = ebgp_propagation
        self.ibgp_propagation = ibgp_propagation

    def compute_dags(self):
        """Compute the propagation graph"""
        # First, group requirements by traffic class: DstNet -> List of Reqs
        net_reqs = {}
        for req in self.reqs:
            if req.dst_net not in net_reqs:
                net_reqs[req.dst_net] = []
            net_reqs[req.dst_net].append(req)

        unmatching_orders = []

        # For each traffic class compute the propagation graph
        for net, reqs in net_reqs.iteritems():
            # for each requirement return the AS paths and router paths (reversed)
            # ebgp_paths, ibgp_paths = self.extract_reqs(reqs)
            as_paths, router_paths = self.extract_reqs(reqs)

            # First compute the propagation among ASes (eBGP propagation)
            # params: self.verify.peering_graph via network_graph
            #         undirected graph, node(x.asnum), edge(x.asnum, y.asnum)
            # params: as_paths, list of set[(origin_asnum, as_paths)] (deduplicate)
            # return: directed graph, requirement paths to allow_path (white list)
            #   (other neighbor path) non-requirement paths to block_path
            ebgp_propagation = compute_propagation(self.verify.peering_graph, as_paths)
            # Second compute the propagation among routers and possibily iBGP Propagation
            ibgp_propagation = compute_propagation(self.network_graph, router_paths)

            print "E" * 50
            print_compute_propagation(ebgp_propagation)
            print "E" * 50
            print "I" * 50
            print_compute_propagation(ibgp_propagation)
            print "I" * 50

            for node in ibgp_propagation.nodes():
                clear = [x for x in ibgp_propagation.node[node]['order'] if x]
                ibgp_propagation.node[node]['order'] = clear

            # check that the path preferenes are implementable by BGP
            # params: ebgp_propagation, directed graph (as number graph via reqs)
            unmatching_order = self.verify.check_order(ebgp_propagation)
            if unmatching_order:
                unmatching_orders.append(unmatching_order)

            # Extend the iBGP propagation to contain the eBGP paths
            self.expand_ebgp_graph(ebgp_propagation, ibgp_propagation, as_paths, router_paths)
            print "I" * 50
            print_compute_propagation(ibgp_propagation)
            print "I" * 50

            self.ebgp_graphs[net] = ebgp_propagation
            self.ibgp_graphs[net] = ibgp_propagation

        self.merge_dags()

        # Override enum
        as_paths = self.partial_eval_propagated_info()

        # print ebgp, ibgp output info
        print as_paths
        self.print_propagation_info()

        self.ctx.create_enum_type(ASPATH_SORT, [get_as_path_key(p) for p in as_paths])
        return unmatching_orders

    def partial_eval_propagated_info(self):
        def get_as_path(path):
            """
            convert router paths to reversed as path and remove consecutive identical asnum
            such as router paths: [R1(as100), R2(as100), R3(as200), R4(as300), R5(as100)]
            return as paths:      (as100, as300, as200, as100) (origin positive direction)
            """
            assert path
            external_peer = None
            egress = None
            peer = None
            # Shortcut
            is_bgp = self.network_graph.is_bgp_enabled
            get_as = self.network_graph.get_bgp_asnum
            if len(path) == 1:
                # Self announcing node
                external_peer = None
                egress = None
                peer = None
                as_path = [get_as(path[0])]
            else:
                as_path = [get_as(path[0])]
                for index, node in enumerate(path):
                    if index == 0:
                        continue
                    prev = path[index - 1]
                    if not is_bgp(node):
                        continue
                    if is_bgp(prev):
                        # node enable bgp and prev enable bgp (ibgp or ebgp)
                        peer = prev
                        if get_as(node) != get_as(prev):
                            external_peer = prev
                            egress = node
                    if not as_path or (as_path and as_path[-1] != get_as(node)):
                        as_path.append(get_as(node))
            return external_peer, egress, peer, tuple(reversed(as_path))

        cache = dict()
        for node in self.ibgp_propagation.nodes():
            for net, attrs in self.ibgp_propagation.node[node]['nets'].iteritems():
                # net: net_prefix -> attrs: info (order, paths, block)
                paths = attrs['paths'].union(attrs['block'])
                for path in paths:
                    if path in cache:
                        continue
                    # convert router paths to reversed as paths
                    # external_peer, the last matching enable bgp external peer
                    # egress, the last matching enable bgp node learned this announcement
                    # peer, the last matching enable bgp internal peer
                    external_peer, egress, peer, as_path = get_as_path(path)
                    # Append any extra AS Path info in the original announcement
                    check = False
                    ann_source = path[0]
                    for ann in self.network_graph.get_bgp_advertise(ann_source):
                        if ann.prefix == net:
                            as_path += tuple(ann.as_path)  # positive direction
                            check = True
                            break
                    # assert announcement source without bgp announcements (same netprefix)
                    err = "Couldn't find announcement {} at node {}," \
                          "current announcements are: {}".format(
                        net, ann_source,
                        self.network_graph.get_bgp_advertise(ann_source))
                    assert check, err
                    # add propagatedinfo to info cache
                    as_path_len = len(as_path)
                    info = PropagatedInfo(
                        external_peer=external_peer,   # the last matching external_peer
                        egress=egress,                 # the last matching egress
                        ann_name=net,                  # network prefix
                        peer=peer,                     # the last matching peer
                        as_path=as_path,               # origin path + announcement path
                        as_path_len=as_path_len,       # added path length
                        path=path)                     # origin paths or block path
                    cache[path] = info

                # according to origin order and hierarchy (order, paths, block)
                # to assign order_info, paths_info, block_info with info cache
                order_info = []
                block_info = set()
                paths_info = set()
                for paths in attrs['order']:
                    new_set = set()
                    for path in paths:
                        info = copy.copy(cache[path])
                        new_set.add(info)
                        paths_info.add(info)
                    order_info.append(new_set)
                for path in attrs['block']:
                    info = copy.copy(cache[path])
                    block_info.add(info)
                attrs['order_info'] = order_info
                attrs['block_info'] = block_info
                attrs['paths_info'] = paths_info

        def find_prev_prop(node, net, propagated):
            assert isinstance(propagated, PropagatedInfo)

            as_num = self.network_graph.get_bgp_asnum(node)
            if len(propagated.path) < 2:
                return None
            neighbor = propagated.peer
            neighbor_as_num = self.network_graph.get_bgp_asnum(neighbor)
            neighbor_attrs = self.ibgp_propagation.node[neighbor]['nets'][net]
            n_paths = neighbor_attrs['paths_info'].union(neighbor_attrs['block_info'])
            for neighbor_info in n_paths:
                assert isinstance(neighbor_info, PropagatedInfo)
                #if propagated.peer != neighbor_info.peer \
                #        and as_num != neighbor_as_num \
                #        and propagated.as_path[1:] != neighbor_info.as_path \
                #        and propagated.path[:-1] != neighbor_info.path:
                #    continue
                #else:
                #    return neighbor_info
                if propagated.path[:-1] == neighbor_info.path:
                    return neighbor_info
            return None

        for node in self.ibgp_propagation.nodes():
            for net, attrs in self.ibgp_propagation.node[node]['nets'].iteritems():
                all_propagated = attrs['paths_info'].union(attrs['block_info'])
                for propagated in all_propagated:
                    if len(propagated.path) < 2:
                        continue
                    origin = find_prev_prop(node, net, propagated)
                    propagated.prev = origin
                    if 'origins' not in attrs:
                        attrs['origins'] = {}
                    attrs['origins'][propagated] = origin

        return set([prop.as_path for prop in cache.values()])

    def synthesize(self, use_igp=False):
        #self.compute_dags()
        for node in self.ibgp_propagation.nodes():
            self.ibgp_propagation.node[node]['box'] = BGP(node, self)
        for node in self.ibgp_propagation.nodes():
            self.ibgp_propagation.node[node]['box'].synthesize(use_igp=use_igp)
            # TODO sub-specifications verifier
            # self.ibgp_propagation.node[node]['box'].synthesize_subspecs()

        print "Y" * 50
        print "PROPAGATION GRAPH SIZE:", self.ibgp_propagation.number_of_nodes()
        print "NETWORK GRAPH SIZE:", self.network_graph.number_of_nodes()
        print "Y" * 50

    def get_generated_ospf_requirements(self):
        reqs = []
        for node in self.ibgp_propagation.nodes():
            box = self.ibgp_propagation.node[node]['box']
            tmp = box.generated_ospf_reqs
            for isequal, p1, p2 in tmp:
                reqs.append((isequal.get_value(), p1, p2))
        return reqs

    def update_network_graph(self):
        """Update the network graph with the concrete values"""
        for node in self.ibgp_propagation.nodes():
            self.ibgp_propagation.node[node]['box'].update_network_graph()

    def print_propagation_info(self):
        """print bgp propagation info"""
        for node in self.ebgp_propagation.nodes():
            print "#" * 50
            print "node:", node
            for net, attrs in self.ebgp_propagation.node[node][NETS].iteritems():
                print "network prefix:", net
                print "paths attributes:", attrs[PATHS]
                print "order attributes:", attrs[ORDER]
                print "block attributes:", attrs[BLOCK]
                print "-" * 50

        for node in self.ibgp_propagation.nodes():
            print "#" * 50
            print "node:", node
            for net, attrs in self.ibgp_propagation.node[node][NETS].iteritems():
                print "network prefix:", net
                print "paths attributes:", attrs[PATHS]
                print "order attributes:", attrs[ORDER]
                print "block attributes:", attrs[BLOCK]
                print "paths info attributes:", attrs[PATHS_INFO]
                print "order info attributes:", attrs[ORDER_INFO]
                print "block info attributes:", attrs[BLOCK_INFO]
                print "origins info attributes:", attrs[ORIGIN]
                print "-" * 50
