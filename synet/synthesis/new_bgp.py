#!/usr/bin/env python

"""synthesize configurations for (e/i)BGP protocol"""

import copy
import logging

import networkx as nx
import z3

from tekton.bgp import Announcement
from tekton.bgp import MatchIpPrefixListList
from tekton.bgp import MatchCommunitiesList
from tekton.graph import NetworkGraph
from synet.utils.fnfree_policy import SMTRouteMap
from synet.utils.fnfree_policy import SMTSetNextHop
from synet.utils.fnfree_policy import SMTMatchNextHop
from synet.utils.fnfree_policy import SMTMatchAll
from synet.utils.fnfree_smt_context import ASPATH_SORT
from synet.utils.fnfree_smt_context import AnnouncementsContext
from synet.utils.fnfree_smt_context import BGP_ORIGIN_SORT
from synet.utils.fnfree_smt_context import NEXT_HOP_SORT
from synet.utils.fnfree_smt_context import PEER_SORT
from synet.utils.fnfree_smt_context import PREFIX_SORT
from synet.utils.fnfree_smt_context import SolverContext
from synet.utils.fnfree_smt_context import is_empty
from synet.utils.fnfree_smt_context import sanitize_smt_name
from synet.utils.smt_context import get_as_path_key


__author__ = "Ahmed El-Hassany"     # maintainer Yongzheng Zhang
__email__ = "a.hassany@gmail.com"   # yongzheng2024@outlook.com


DEFAULT_LOCAL_PREF = 100
DEFAULT_MED = 100


def get_propagated_info(propagation_graph, node, prefix=None, unselected=True, 
                        igp_pass=False, from_node=None, from_peer=None):
    all_props = set()
    if not propagation_graph.has_node(node):
        return all_props
    for net, data in propagation_graph.node[node]['nets'].iteritems():
        if prefix and net != prefix:
            continue
        for propgated in data['paths_info']:
            all_props.add(propgated)
        if not unselected:
            continue
        for propgated in data['block_info']:
            all_props.add(propgated)
        # if not igp_pass:
        #     continue
        # for prop in data['prop_igp_pass']:
        #     all_props.append(prop)
    # if not from_node:
    #     return all_props
    ret = set()
    for propgated in all_props:
        if from_node:
            if len(propgated.path) < 2:
                continue
            if propgated.path[-2] != from_node:
                continue
        if from_peer:
            if propgated.peer != from_peer:
                continue
        ret.add(propgated)
    return ret


def create_sym_ann(ctx, fixed_values=None, name_prefix=None):
    """Return the new symbolic announcement announcement"""
    if not fixed_values:
        fixed_values = {}
    vals = {}
    all_attrs = [
        ('prefix', PREFIX_SORT, None),
        ('peer', PEER_SORT, None),
        ('origin', BGP_ORIGIN_SORT, lambda x: x.name),
        ('as_path', ASPATH_SORT, get_as_path_key),
        ('as_path_len', z3.IntSort(ctx.z3_ctx), None),
        ('next_hop', NEXT_HOP_SORT, None),
        ('local_pref', z3.IntSort(ctx.z3_ctx), None),
        ('med', z3.IntSort(ctx.z3_ctx), None),
        ('permitted', z3.BoolSort(ctx.z3_ctx), None),
    ]
    print "$" * 50
    for attr, vsort, conv in all_attrs:
        is_enum = isinstance(vsort, basestring)
        value = None
        if is_enum:
            vsort = ctx.get_enum_type(vsort)
        if attr in fixed_values:
            if is_enum:
                value = vsort.get_symbolic_value(sanitize_smt_name(fixed_values[attr]))
            else:
                value = fixed_values[attr]
        nprefix = "%s_" % attr
        nprefix = "%s_%s" % (name_prefix, nprefix) if name_prefix else nprefix
        vals[attr] = ctx.create_fresh_var(vsort=vsort, value=value, name_prefix=nprefix)
        print "CREATED", vals[attr]
        print value
    comms = 'communities'
    vals[comms] = {}
    for community in ctx.communities:
        value = fixed_values.get(comms, {}).get(community, None)
        nprefix = "Comm_%s_" % str(community).replace(":", "_")
        nprefix = "%s_%s" % (name_prefix, nprefix) if name_prefix else nprefix
        comm_var = ctx.create_fresh_var(
            vsort=z3.BoolSort(ctx.z3_ctx),
            value=value,
            name_prefix=nprefix)
        vals['communities'][community] = comm_var
        print "CREATED", comm_var
        print value
    print "$" * 50
    new_ann = Announcement(**vals)
    return new_ann


def assert_order(old, new):
    if old == new:
        return True
    elif new is None:
        return False
    else:
        return assert_order(old, new.prev_announcement)


class BGP(object):
    def __init__(self, node, propagation):
        log_name = '%s.%s' % (self.__module__, self.__class__.__name__)
        self.log = logging.getLogger(log_name)
        self.node = node
        self.propagation = propagation
        self.ctx = self.propagation.ctx
        assert isinstance(self.ctx, SolverContext)
        self.network_graph = self.propagation.network_graph
        assert isinstance(self.network_graph, NetworkGraph)
        self.next_hop_map = self.propagation.next_hop_map
        self.peering_graph = self.propagation.verify.peering_graph
        self.ebgp_propagation = self.propagation.ebgp_propagation
        self.ibgp_propagation = self.propagation.ibgp_propagation
        assert isinstance(self.peering_graph, nx.Graph)
        assert isinstance(self.ebgp_propagation, nx.Graph)
        assert isinstance(self.ibgp_propagation, nx.Graph)
        self.rmaps = {}
        # Symbolic variables of all (possibly) learned announcements
        self.anns_map = self.create_symbolic_announcements()
        # The context for all (possibly) learned announcements
        # paths_info + block_info
        self.anns_ctx = AnnouncementsContext(self.anns_map.values(), mutators=[self])
        # Only the subset of announcement that are used to
        # (possibly) forward traffic, paths_info
        self.selected_sham = self._get_selected_sham()
        # The set of PropagatedInfo that will be exported to neighbors
        self.exported_routes = self.compute_exported_routes()
        self.export_ctx = {}
        self.generated_ospf_reqs = []
        self._cache = {}
        self.selection_constraints = {}  # Cache constraints used for the BGP selection

    def create_symbolic_announcements(self):
        """
        Create symbolic variables of all (possibly) learned announcements
        :return dict PropagationInfo -> Symbolic Announcement
        """
        anns_map = dict()
        # all_anns: set of paths_info and block_info
        all_anns = get_propagated_info(self.ibgp_propagation, self.node, unselected=True)
        for propagated in all_anns:
            fixed = {'prefix': propagated.ann_name}
            # Partial eval peer
            fixed['peer'] = self.node if len(propagated.path) == 1 else propagated.peer
            # TODO: support more origins
            fixed['origin'] = 'EBGP'
            # TODO: parial eval AS path
            # TODO support as path rewrites
            fixed['as_path'] = get_as_path_key(propagated.as_path)
            fixed['as_path_len'] = len(propagated.as_path) - 1
            if len(propagated.path) == 1:
                origin_anns = None
                for tt in self.network_graph.get_bgp_advertise(propagated.path[0]):
                    if propagated.ann_name == tt.prefix:
                        origin_anns = tt
                        break
                fixed['next_hop'] = self.ctx.origin_next_hop
                if origin_anns:
                    fixed['local_pref'] = origin_anns.local_pref
                    fixed['med'] = origin_anns.med
                    fixed['communities'] = {}
                    for community in self.ctx.communities:
                        fixed['communities'][community] = origin_anns.communities[community]
                else:
                    fixed['local_pref'] = DEFAULT_LOCAL_PREF
                    fixed['med'] = DEFAULT_MED
                    fixed['communities'] = {}
                    # TODO: community False or True meaning ??
                    for community in self.ctx.communities:
                        fixed['communities'][community] = False
            name_prefix = "Sham_{}_{}_from_{}".format(self.node, propagated.ann_name, propagated.peer)
            # print "$" * 50
            # print name_prefix
            # print "$" * 50
            new_ann = create_sym_ann(self.ctx, fixed, name_prefix=name_prefix)
            anns_map[propagated] = new_ann
        return anns_map

    def compute_exported_routes(self):
        """
        Compute the routes to be exported on each outgoing edge of the router
        """
        self.log.debug("compute_exported_routes at %s", self.node)

        # First compute what is exported to each neighbor
        # neighbor -> exported_info, propagation info from this node (from_peer)
        exported_info = {}
        for neighbor in self.network_graph.get_bgp_neighbors(self.node):
            # Announcement that the neighbor will learn from this router
            # all_anns: set of paths_info and block_info from this node (from_peer)
            all_anns = get_propagated_info(self.ibgp_propagation, neighbor,
                                           unselected=True, igp_pass=False, 
                                           from_peer=self.node)
            for prop in all_anns:
                if neighbor not in exported_info:
                    exported_info[neighbor] = []
                exported_info[neighbor].append(prop)

        for peer, props in exported_info.iteritems():
            self.log.debug("Node %s Exported to %s: %s", self.node, peer, props)

        # Second, map the propagated to the local announcements
        # neighbor -> exported_anns
        export_anns = {}
        for neighbor, propagated in exported_info.iteritems():
            n_attrs = self.ibgp_propagation.node[neighbor]
            if neighbor not in export_anns:
                export_anns[neighbor] = {}
            for prop in propagated:
                origin = n_attrs['nets'][prop.ann_name]['origins'][prop]
                # print "O" * 50
                # print prop
                # print origin
                # print "O" * 50
                if not origin:
                    continue
                export_anns[neighbor][prop] = self.anns_map[origin]
            if not export_anns[neighbor]:
                del export_anns[neighbor]

        # R2 -> export R1      y
        # R1 <- import R2      x         block + paths

        # [Provider2, R2, R1, Provider1]_info
        # [Provider2, R2, R1]_info     origin

        # paths info + block info ( + order info )
        # BGP SMT begin: xxxxx_info -> xxxxx_anns ( SMT variables )

        # export_info[neighbor][prop] -> origin xxxxx_info
        # export_anns[neighbor][prop] -> origin xxxxx_anns

        # TODO write some infomation to related file
        # Third, apply export route map (if any)
        for neighbor, vals in export_anns.iteritems():
            # Since the announcements will change
            # We try to keep the ordering
            props = []
            anns = []
            for prop, ann in vals.iteritems():
                props.append(prop)
                anns.append(ann)

            # Apply any export policies (if any)
            rmap_name = self.network_graph.get_bgp_export_route_map(self.node, neighbor)
            print "E" * 80
            print rmap_name
            for prop in props:
                print prop
            print "E" * 80
            if not rmap_name:
                continue
            rmap = self.network_graph.get_route_maps(self.node)[rmap_name]
            tmp = self.anns_ctx.create_new(anns, self.compute_exported_routes)
            smt_map = SMTRouteMap(rmap, tmp, self.ctx)
            self.rmaps[rmap_name] = smt_map
            smt_map.execute()    # pass
            for index, prop in enumerate(props):
                # update export_anns[neighbor][prop]
                #        origin -> route map (smt_map.announcements[index])
                export_anns[neighbor][prop] = smt_map.announcements[index]
                # print "E" * 50
                # print index, prop
                # print smt_map.announcements[index]
                # print "E" * 50
                assert assert_order(tmp[index], export_anns[neighbor][prop])

        print "A" * 70
        print export_anns
        print "A" * 70
        return export_anns

    def _get_selected_sham(self):
        """
        To resolve circular dependencies of getting the context generate
        by each router
        First generate a context with all symbolic variables.
        Then it will be glued in the selection process to concrete values
        in the SMT Sovler
        :return: AnnouncementsContext
        """
        # selected: set of paths_info
        selected = get_propagated_info(self.ibgp_propagation, self.node, unselected=False)
        # print "SELECTED AT", self.node
        for s in selected:
            self.log.debug("Create selected sham at router '%s': %s", self.node, str(s))
        anns = [self.anns_map[propagated] for propagated in selected]
        return self.anns_ctx.create_new(anns, mutator=self._get_selected_sham)

    def compute_imported_routes(self):
        #attrs = ['prefix', 'peer', 'origin', 'as_path', 'as_path_len',
        #         'next_hop', 'local_pref', 'med', 'permitted']
        # The attributes that are read from the neighbor
        attrs = ['prefix', 'next_hop', 'origin', 'local_pref', 'med', 'permitted']
        for neighbor in self.network_graph.get_bgp_neighbors(self.node):
            if not self.ibgp_propagation.has_node(neighbor):
                continue
            asnum = self.network_graph.get_bgp_asnum(self.node)
            neighbor_asnum = self.network_graph.get_bgp_asnum(neighbor)
            is_ebgp_neighbor = asnum != neighbor_asnum
            n_attrs = self.ibgp_propagation.node[neighbor]
            neighbor_exported = n_attrs['box'].exported_routes
            if self.node not in neighbor_exported:
                # The neighbor doesn't export anything to this router
                self.log.debug("NODE %s doesn't import anything from %s: %s",
                               self.node, neighbor, neighbor_exported.keys())
                continue
            imported = {}
            for prop, ann in neighbor_exported[self.node].iteritems():
                assert prop in self.anns_map
                ann = copy.copy(ann)  # Shallow copy
                next_hop_sort = self.ctx.get_enum_type(NEXT_HOP_SORT)
                next_hop = self.next_hop_map[self.node][neighbor]
                if is_ebgp_neighbor:
                    ann.local_pref = self.ctx.create_fresh_var(
                        z3.IntSort(self.ctx.z3_ctx),
                        value=DEFAULT_LOCAL_PREF)
                    next_hop_var = self.ctx.create_fresh_var(next_hop_sort, value=next_hop)
                    ann.next_hop = next_hop_var
                    self._cache[(self.node, neighbor)] = (True, ann.next_hop, next_hop_var)
                else:
                    next_hop_var = self.ctx.create_fresh_var(next_hop_sort, value=None)
                    prev_next_hop = ann.next_hop
                    ann.next_hop = next_hop_var
                    self.ctx.register_constraint(
                        z3.If(prev_next_hop.var == self.ctx.origin_next_hop_var,
                              next_hop_var.var == next_hop_sort.get_symbolic_value(next_hop),
                              next_hop_var.var == prev_next_hop.var,
                              self.ctx.z3_ctx) == True)
                imported[prop] = ann

            # Apply import route maps if any
            rmap_name = self.network_graph.get_bgp_import_route_map(self.node, neighbor)
            if rmap_name:
                rmap = self.network_graph.get_route_maps(self.node)[rmap_name]
                # Since the announcements will change
                # We try to keep the ordering
                props = []
                anns = []
                for prop, ann in imported.iteritems():
                    props.append(prop)
                    anns.append(ann)
                tmp = self.anns_ctx.create_new(anns, self.compute_exported_routes)
                smt_map = SMTRouteMap(rmap, tmp, self.ctx)
                self.rmaps[rmap_name] = smt_map
                smt_map.execute()
                cc = self.ctx._tracked.keys()[:]
                for index, prop in enumerate(props):
                    imported[prop] = smt_map.announcements[index]
                    assert assert_order(tmp[index], imported[prop])
            # Assign the values
            for prop, ann in imported.iteritems():
                self.anns_map[prop].prev_announcement = ann
                for attr in attrs:
                    curr = getattr(self.anns_map[prop], attr)
                    imp = getattr(ann, attr)
                    prefix = 'Imp_%s_from_%s_%s_' % (self.node, neighbor, attr)
                    self.ctx.register_constraint(z3.And(curr.var == imp.var, self.ctx.z3_ctx),
                                                 name_prefix=prefix)
                for community in self.ctx.communities:
                    curr = self.anns_map[prop].communities[community]
                    imp = ann.communities[community]
                    prefix = 'Imp_%s_from_%s_Comm_%s_' % (self.node, neighbor, community.name)
                    self.ctx.register_constraint(z3.And(curr.var == imp.var, self.ctx.z3_ctx), name_prefix=prefix)

    def get_path_cost(self, path):
        """
        Get the IGP path cost for a given path
        Currently only reads OSPF costs
        """
        costs = []
        inverse = list(reversed(path))
        current_as = self.network_graph.get_bgp_asnum(self.node)
        sub_path = [inverse[0]]
        for src, dst in zip(inverse[0::1], inverse[1::1]):
            if self.network_graph.is_bgp_enabled(dst):
                dst_as = self.network_graph.get_bgp_asnum(dst)
            else:
                dst_as = current_as
            if dst_as != current_as:
                break
            cost = self.network_graph.get_edge_ospf_cost(src, dst)
            if is_empty(cost):
                prefix = "_{}_{}_".format(src, dst)
                cost = self.ctx.create_fresh_var(
                    z3.IntSort(self.ctx.z3_ctx),
                    name_prefix="IGP_edge_cost_{}".format(prefix))
                self.ctx.register_constraint(
                    cost.var > 0,
                    name_prefix="positive_igp_cost_{}".format(prefix))
            sub_path.append(dst)
            costs.append(cost)
        concrete = [cost for cost in costs if isinstance(cost, int)]
        variables = [cost.var for cost in costs if hasattr(cost, 'var')]
        # Assert we read everything
        assert len(variables) + len(concrete) == len(costs)
        if concrete or variables:
            all_costs = concrete + variables # + [self.ctx.z3_ctx]
            summed = z3.Sum(*all_costs)
            return summed, sub_path
        else:
            return 0, None

    def selector_func(self, best_propagated, best_ann_var,
                      other_propagated, other_ann_var, use_igp=False):
        """Synthesize Selection function for a given prefix"""
        self.log.debug(
            "prefix_select %s at %s, best=%s", best_propagated.ann_name, self.node, best_propagated)
        if best_propagated.path:
            best_neighbor = best_propagated.path[-2]
        else:
            best_neighbor = None
        other_neighbor = other_propagated.path[-2]
        if best_propagated.peer == other_propagated.peer:
            return

        as_len_enabled = True # self.get_as_len_enabled()
        const_set = []
        const_selection = []

        self.log.debug("select at %s: %s over %s",
                       self.node, best_propagated, other_propagated)

        best_peer = best_propagated.peer
        peer = other_propagated.peer

        s_localpref = best_ann_var.local_pref.var
        o_localpref = other_ann_var.local_pref.var

        s_aslen = best_ann_var.as_path_len.var
        o_aslen = other_ann_var.as_path_len.var

        s_origin = best_ann_var.origin.var
        o_origin = other_ann_var.origin.var

        origin_sort = self.ctx.get_enum_type(BGP_ORIGIN_SORT)
        igp_origin = origin_sort.get_symbolic_value('IGP')
        ebgp_origin = origin_sort.get_symbolic_value('EBGP')
        incomplete_origin = origin_sort.get_symbolic_value('INCOMPLETE')

        best_as_num = self.network_graph.get_bgp_asnum(best_peer)
        other_as_num = self.network_graph.get_bgp_asnum(peer)
        node_as_num = self.network_graph.get_bgp_asnum(self.node)

        other_permitted = other_ann_var.permitted.var

        # Selection based on origin
        select_origin = z3.Or(
            # IGP is the lowest
            z3.And(s_origin == igp_origin,
                   o_origin != igp_origin, self.ctx.z3_ctx),
            # EGP over incomplete
            z3.And(s_origin == ebgp_origin,
                   o_origin == incomplete_origin, self.ctx.z3_ctx),
            self.ctx.z3_ctx)

        # Prefer eBGP routes over iBGP
        select_ebgp = z3.And(node_as_num != best_as_num,
                             node_as_num == other_as_num,
                             self.ctx.z3_ctx)

        # MED
        select_med = z3.And(best_as_num == other_as_num,
                            best_ann_var.med.var < other_ann_var.med.var, self.ctx.z3_ctx)
        not_select_med = z3.Or(best_as_num != other_as_num,
                               z3.And(best_as_num == other_as_num,
                                      best_ann_var.med.var == other_ann_var.med.var, self.ctx.z3_ctx),
                               self.ctx.z3_ctx)
        # IGP
        prefix = "igp_{}_is_equal_{}".format("_".join(best_propagated.path), "_".join(other_propagated.path))
        igp_path_equal = self.ctx.create_fresh_var(z3.BoolSort(self.ctx.z3_ctx), name_prefix=prefix)
        if use_igp:
            best_igp_cost, best_sub_path = self.get_path_cost(best_propagated.path)
            other_igp_cost, other_sub_path = self.get_path_cost(other_propagated.path)

            if best_sub_path and other_sub_path:
                self.generated_ospf_reqs.append((igp_path_equal, best_sub_path, other_sub_path))
        else:
            # Force the opposite selection
            best_igp_cost = 15
            other_igp_cost = 10

        # Selection based on router IDs
        best_router_id = self.network_graph.get_bgp_router_id(best_neighbor)
        if not best_router_id:
            self.log.warn("Router ID is not set for {} {}".format(best_neighbor, best_router_id))
        other_router_id = self.network_graph.get_bgp_router_id(other_neighbor)
        if not other_router_id:
            self.log.warn("Router ID is not set for {} {}".format(other_neighbor, other_router_id))
        if best_router_id and other_router_id:
            # Router ID are known, we can make assumptions about them
            select_router_id = best_router_id.var < other_router_id.var
        else:
            # Router IDs are NOT known, assume they're not in our favor
            select_router_id = self.ctx.create_fresh_var(
                z3.BoolSort(self.ctx.z3_ctx),
                value=False,
                name_prefix='SelectRouterID_{}_'.format(self.node)).var

        # The BGP selection process
        const_selection.append(
            z3.Or(
                # 1) Permitted
                other_permitted == False,
                # 2) If Permitted, local pref
                z3.And(other_permitted,
                       s_localpref > o_localpref,
                       self.ctx.z3_ctx),
                # 3) AS Path Length
                z3.And(other_permitted,
                       s_localpref == o_localpref,
                       s_aslen < o_aslen,
                       self.ctx.z3_ctx),
                # 4) Origin Code IGP < EGP < Incomplete
                z3.And(other_permitted,
                       s_localpref == o_localpref,
                       s_aslen == o_aslen,
                       select_origin == True,
                       self.ctx.z3_ctx),
                # 5) MED Selection
                z3.And(
                    other_permitted,
                    s_localpref == o_localpref,
                    s_aslen == o_aslen,
                    select_origin == False,
                    select_med == True,
                    self.ctx.z3_ctx),
                # 6) Prefer eBGP over iBGP paths.
                z3.And(
                    other_permitted,
                    s_localpref == o_localpref,
                    s_aslen == o_aslen,
                    select_origin == False,
                    select_med == False,
                    not_select_med == True,
                    select_ebgp == True,
                    self.ctx.z3_ctx),
                # 7) Path with the lowest IGP metric to the BGP next hop.
                z3.And(
                    other_permitted,
                    s_localpref == o_localpref,
                    s_aslen == o_aslen,
                    select_origin == False,
                    select_med == False,
                    not_select_med == True,
                    select_ebgp == False,
                    use_igp == True,
                    igp_path_equal.var == False,
                    best_igp_cost < other_igp_cost,
                    self.ctx.z3_ctx
                ),
                # TODO (AH): More selection process
                # 8) Determine if multiple paths
                #    require installation in the
                #    routing table for BGP Multipath.
                #      Continue, if bestpath is not yet selected.
                # 9) Router IDs
                z3.And(
                    other_permitted,
                    s_localpref == o_localpref,
                    s_aslen == o_aslen,
                    select_origin == False,
                    select_med == False,
                    not_select_med == True,
                    select_ebgp == False,
                    use_igp == True,
                    best_igp_cost == other_igp_cost,
                    select_router_id == True,
                    igp_path_equal.var == True,
                    self.ctx.z3_ctx
                ),
                self.ctx.z3_ctx,
            ))

        tmp = const_selection + [self.ctx.z3_ctx]
        prefix = "SELECT_at_{}_prefix_{}_path_{}_".format(
            self.node, best_propagated.ann_name, '_'.join(best_propagated.path))
        const_name = self.ctx.register_constraint(z3.And(*tmp) == True, name_prefix=prefix)
        self.selection_constraints[const_name] = (best_ann_var, other_ann_var, best_propagated, other_propagated, const_selection)

    def mark_selected(self):
        for propagated, ann in self.anns_map.iteritems():
            n = '_{}_from_{}_path_{}_'.format(self.node, propagated.peer, '_'.join(propagated.path))
            if ann not in self.selected_sham:
                self.ctx.register_constraint(ann.permitted.var == False, name_prefix='Req_Block' + n)
            else:
                self.ctx.register_constraint(ann.permitted.var == True, name_prefix='Req_Allow' + n)

    def synthesize(self, use_igp=False):

        # network topology 
        # requirements + announcements
        # configuration sketch -> import and export route map
        #        ==========> SMT

        # prefix_list, nexthop_list, peering_list, .... -> enum, z3.EnumSort....

        # input: order requirements + announcements
        # output: 
        #   paths, order, block                (direction, provider -> customer)
        #   paths_info, order_info, block_info (+announcements, direction)
        #   origins (path1: origin_path1)

        # Provider1, R1, R2, Provider2   ---> origin       <-------+ export route map
        # Provider1, R1, R2 +--------------------------------------+

        # this node all paths info + block info -> SMT value +-----+
        # announcements (UPDATE message) -> SMT value      <-------+

        # TODO hole within configuration sketch
        # export route map -> SMTRouteMap, SMTRouteMapLine +-------+
        #                                                          |
        #                                                          |
        # path info Allow, block info Block                        |
        # import route map -> SMT Import RouteMap <----------------+
        # 
        # 
        # requirements order -> SMT ...


        # -------------> SMT solver & SMT check
        # -------------> SMT output smt.smt2

        self.log.info("Synthesizing BGP for router '%s'", self.node)
        self.mark_selected()
        self.compute_imported_routes()

        anns_order = {}
        for net, info in self.ibgp_propagation.node[self.node]['nets'].iteritems():
            if net not in anns_order:
                anns_order[net] = []
            anns_order[net] = info['order_info']

        for ann_name, values in anns_order.iteritems():
            if len(values) == 1:
                # This router only learns one route
                # No need to use the preference function
                continue
            for best_prop_set, other_prop_set in zip(values[0::1], values[1::1]):
                for best_prop in best_prop_set:
                    for other_prop in other_prop_set:
                        best_ann = self.anns_map[best_prop]
                        other_ann = self.anns_map[other_prop]
                        self.selector_func(best_prop, best_ann, other_prop,
                                           other_ann, use_igp=use_igp)

    def synthesize_subspecs(self):
        self.log.info("Synthesizing BGP sub-specifications for router '%s'", self.node) 
        self.mark_selected()
        self.compute_imported_routes()

    def get_config(self):
        """Get concrete route configs"""
        configs = []
        for smt_rmap in self.rmaps.values():
            configs.append(smt_rmap.get_config())
        return configs

    def update_network_graph(self):
        """Update the network graph with the concrete values"""
        for smt_rmap in self.rmaps.values():
            rmap = smt_rmap.get_config()
            print "P" * 50
            print rmap
            print "P" * 50
            self.network_graph.add_route_map(self.node, rmap)
            for line in rmap.lines:
                for match in line.matches:
                    if isinstance(match, MatchIpPrefixListList):
                        try:
                            self.network_graph.del_ip_prefix_list(self.node, match.match)
                        except Exception as exp:
                            pass
                        self.network_graph.add_ip_prefix_list(self.node, match.match)
                    elif isinstance(match, MatchCommunitiesList):
                        try:
                            self.network_graph.del_community_list(self.node, match.match)
                        except Exception as exp:
                            pass
                        self.network_graph.add_bgp_community_list(self.node, match.match)
        router_id = self.network_graph.get_bgp_router_id(self.node)
        if router_id and router_id.is_concrete:
            self.network_graph.set_bgp_router_id(self.node, router_id.get_value())
        else:
            self.network_graph.set_bgp_router_id(self.node, None)
