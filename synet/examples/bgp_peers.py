#!/usr/bin/env python

"""
An Simple example of an AS with two providers and one customer
The policy is such that the customer traffic prefer once provider over the other
And providers cannot use the network as transit.

       +-------------+        +-------------+
       | Provider1   |        | Provider2   |     Provider1, Provider2
       | 10.0.0.2    |        | 10.0.0.4    |     network1: 128.0.0.0/24
       +-------------+        +-------------+
              | AS 400               | AS 500
              |                      |
    +---------|----------------------|---------+
    |  +-------------+        +-------------+  |  Routing Policy
    |  | R2          |--------| R3          |  |  Rule 1: Traffic from the customer peer
    |  | 192.168.1.1 |        | 192.168.0.1 |  |  AS600 to the external peers prefers exit
    |  +-------------+        +-------------+  |  routers in order: AS400, AS500
    |         |                      |         |
    |         |    +-------------+   |         |
    |         +----| R1          |---+         |
    |              | 192.168.2.1 |             |
    |              +-------------+      AS 100 |
    +---------------------|--------------------+
                          |
                   +-------------+
                   | Cutomer     |                Customer
                   | 10.0.0.0    |                network2: 128.0.1.0/24
                   +-------------+ AS 600

    Customer  (Fa0/0 10.0.0.0/31) <--> R1 (Fa0/0 10.0.0.1/31)
    Customer  (lo100 128.0.1.1/32)

    Provider1 (Fa0/0 10.0.0.2/31) <--> R2 (Fa0/0 10.0.0.3/31)
    Provider1 (lo100 128.0.0.1/32)

    Provider2 (Fa0/0 10.0.0.4/31) <--> R3 (Fa0/0 10.0.0.5/31)
    Provider2 (lo100 128.0.0.1/32)

    R1 (Fa0/0 10.0.0.1/31)  <--> Customer  (Fa0/0 10.0.0.0/31)
    R1 (Fa0/1 10.0.0.11/31) <--> R2 (Fa0/1 10.0.0.10/31)
    R1 (Fa1/0 10.0.0.7/31)  <--> R3 (Fa0/1 10.0.0.6/31)
    R1 (lo100 192.168.2.1/32)

    R2 (Fa0/0 10.0.0.3/31)  <--> Provider1 (Fa0/0 10.0.0.2/31)
    R2 (Fa0/1 10.0.0.10/31) <--> R1 (Fa0/1 10.0.0.11/31)
    R2 (Fa1/0 10.0.0.9/31)  <--> R3 (Fa1/0 10.0.0.8/31)
    R2 (lo100 192.168.1.1/32)

    R3 (Fa0/0 10.0.0.5/31)  <--> Provider2 (Fa0/0 10.0.0.4/31)
    R3 (Fa0/1 10.0.0.6/31)  <--> R1 (Fa1/0 10.0.0.7/31)
    R3 (Fa1/0 10.0.0.8/31)  <--> R2 (Fa1/0 10.0.0.9/31)
    R3 (lo100 192.168.0.1/32)
"""

import argparse
import logging
from ipaddress import ip_interface
from ipaddress import ip_network
import json

from synet.utils.common import PathReq
from synet.utils.common import PathOrderReq
from synet.utils.common import KConnectedPathsReq
from synet.utils.common import Protocols

from tekton.utils import VALUENOTSET

from tekton.bgp import BGP_ATTRS_ORIGIN
from tekton.bgp import RouteMapLine
from tekton.bgp import RouteMap
from tekton.bgp import Announcement
from tekton.bgp import Community
from synet.netcomplete import NetComplete
from synet.utils.topo_gen import gen_mesh


def setup_logging():
    # create logger
    logger = logging.getLogger('synet')
    logger.setLevel(logging.DEBUG)

    # create console handler and set level to debug
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)

    # create formatter
    formatter = logging.Formatter('%(name)s - %(levelname)s - %(message)s')

    # add formatter to ch
    ch.setFormatter(formatter)

    # add ch to logger
    logger.addHandler(ch)


def bgp_example(output_dir):
    # Generate the basic network of three routers
    # generate a full mesh topology, mesh_size = 3, asnum = 100
    graph = gen_mesh(3, 100)
    # node: R1, R2, R3
    r1, r2, r3 = 'R1', 'R2', 'R3'

    # Enable OSPF in the sketch
    for node in graph.local_routers_iter():
        graph.enable_ospf(node, 100)
    # Edge weights are symbolic
    for src, dst in graph.edges():
        graph.set_edge_ospf_cost(src, dst, VALUENOTSET)
    graph.set_loopback_addr(r3, 'lo100', VALUENOTSET)
    graph.set_loopback_addr(r2, 'lo100', VALUENOTSET)
    graph.add_ospf_network(r1, 'lo100', '0.0.0.0')
    graph.add_ospf_network(r2, 'lo100', '0.0.0.0')
    graph.add_ospf_network(r3, 'lo100', '0.0.0.0')
    graph.add_ospf_network(r1, 'Fa0/0', '0.0.0.0')
    graph.add_ospf_network(r2, 'Fa0/0', '0.0.0.0')
    graph.add_ospf_network(r3, 'Fa0/0', '0.0.0.0')

    # Add two providers and one customer
    provider1 = 'Provider1'
    provider2 = 'Provider2'
    customer = 'Customer'
    graph.add_peer(provider1)
    graph.add_peer(provider2)
    graph.add_peer(customer)
    graph.set_bgp_asnum(provider1, 400)
    graph.set_bgp_asnum(provider2, 500)
    graph.set_bgp_asnum(customer, 600)
    graph.add_peer_edge(r2, provider1)
    graph.add_peer_edge(provider1, r2)
    graph.add_peer_edge(r3, provider2)
    graph.add_peer_edge(provider2, r3)
    graph.add_peer_edge(r1, customer)
    graph.add_peer_edge(customer, r1)

    # Establish BGP peering
    graph.add_bgp_neighbor(provider1, r2)
    graph.add_bgp_neighbor(provider2, r3)
    graph.add_bgp_neighbor(customer, r1)

    # The traffic class announced by the two providers
    net1 = ip_network(u'128.0.0.0/24')
    # The traffic class announced by the customer
    net2 = ip_network(u'128.0.1.0/24')

    prefix1 = str(net1)
    prefix2 = str(net2)
    # Known communities
    comms = [Community("100:{}".format(c)) for c in range(1, 4)]
    # The symbolic announcement injected by provider1
    ann1 = Announcement(prefix1,
                        peer=provider1,
                        origin=BGP_ATTRS_ORIGIN.INCOMPLETE,
                        as_path=[5000],  # We assume it learned from other upstream ASes
                        as_path_len=1,
                        #next_hop='0.0.0.0',
                        next_hop='{}Hop'.format(provider1),
                        local_pref=100,
                        med=100,
                        communities=dict([(c, False) for c in comms]),
                        permitted=True)
    # The symbolic announcement injected by provider1
    # Note it has a shorter AS Path
    ann2 = Announcement(prefix1,
                        peer=provider2,
                        origin=BGP_ATTRS_ORIGIN.INCOMPLETE,
                        as_path=[3000, 5000],  # We assume it learned from other upstream ASes
                        as_path_len=2,
                        next_hop='0.0.0.0',
                        local_pref=100,
                        med=100,
                        communities=dict([(c, False) for c in comms]),
                        permitted=True)
    # The symbolic announcement injected by customer
    ann3 = Announcement(prefix2,
                        peer=customer,
                        origin=BGP_ATTRS_ORIGIN.INCOMPLETE,
                        as_path=[],
                        as_path_len=0,
                        next_hop='0.0.0.0',
                        local_pref=100,
                        med=100,
                        communities=dict([(c, False) for c in comms]),
                        permitted=True)

    graph.add_bgp_advertise(provider1, ann1, loopback='lo100')
    graph.set_loopback_addr(provider1, 'lo100', ip_interface(net1.hosts().next()))

    graph.add_bgp_advertise(provider2, ann2, loopback='lo100')
    graph.set_loopback_addr(provider2, 'lo100', ip_interface(net1.hosts().next()))

    graph.add_bgp_advertise(customer, ann3, loopback='lo100')
    graph.set_loopback_addr(customer, 'lo100', ip_interface(net2.hosts().next()))

    ########################## Configuration sketch ###############################

    # modified by yongzheng for add (r1, customer) cofigure sketch
    # for local, peer in [(r2, provider1), (r3, provider2)]:
    for local, peer in [(r1, customer), (r2, provider1), (r3, provider2)]:
        imp_name = "{}_import_from_{}".format(local, peer)
        exp_name = "{}_export_to_{}".format(local, peer)
        # generate route map
        imp = RouteMap.generate_symbolic(name=imp_name, graph=graph, router=local)
        exp = RouteMap.generate_symbolic(name=exp_name, graph=graph, router=local)
        graph.add_bgp_import_route_map(local, peer, imp.name)
        graph.add_bgp_export_route_map(local, peer, exp.name)

    for local, peer in [(r2, r3), (r3, r2)]:
        # In Cisco the last line is a drop by default
        # configure route-map R2_export_R3 permit 10
        rline1 = RouteMapLine(matches=[], actions=[], access=VALUENOTSET, lineno=10)
        from tekton.bgp import Access
        # configure route-map R2_export_R3 deny 100
        rline2 = RouteMapLine(matches=[], actions=[], access=Access.deny, lineno=100)
        rmap_export = RouteMap(name='{}_export_{}'.format(local, peer), lines=[rline1, rline2])
        graph.add_route_map(local, rmap_export)
        graph.add_bgp_export_route_map(local, peer, rmap_export.name)

    ############################### Requirements ##################################

    path1 = PathReq(Protocols.BGP, prefix1, [customer, r1, r2, provider1], False)
    path2 = PathReq(Protocols.BGP, prefix1, [customer, r1, r3, r2, provider1], False)
    path3 = PathReq(Protocols.BGP, prefix1, [r3, r1, r2, provider1], False)

    path4 = PathReq(Protocols.BGP, prefix1, [customer, r1, r3, provider2], False)
    path5 = PathReq(Protocols.BGP, prefix1, [customer, r1, r2, r3, provider2], False)
    path6 = PathReq(Protocols.BGP, prefix1, [r2, r1, r3, provider2], False)

    reqs = [
        PathOrderReq(
            Protocols.BGP,
            prefix1,
            [
                KConnectedPathsReq(Protocols.BGP, prefix1, [path1, path2, path3], False),
                KConnectedPathsReq(Protocols.BGP, prefix1, [path4, path5, path6], False),
                # path1,
                # path4
            ],
            False),
        PathOrderReq(
            Protocols.OSPF,
            "dummy",
            [
                PathReq(Protocols.OSPF, "dummy", [r1, r2], False),
                PathReq(Protocols.OSPF, "dummy", [r1, r3, r2], False),
            ],
            False
        ),
        PathOrderReq(
            Protocols.OSPF,
            "dummy",
            [
                PathReq(Protocols.OSPF, "dummy", [r1, r3], False),
                PathReq(Protocols.OSPF, "dummy", [r1, r2, r3], False),
            ],
            False
        ),
    ]

    ############################### Print Graph ###################################

    graph.write_dot('out-graph/dot_file')
    graph.write_graphml('out-graph/graphml_file')
    graph.write_propane('out-graph/propane_file')

    r1_route_maps_dict = graph.get_route_maps(r1)
    print "=" * 20 + " r1 route maps " + "=" * 15
    print r1_route_maps_dict

    r2_route_maps_dict = graph.get_route_maps(r2)
    print "=" * 20 + " r2 route maps " + "=" * 15
    print r2_route_maps_dict
    r2_ip_prefix_dict = graph.get_ip_preflix_lists(r2)
    print "=" * 20 + " r2 ip prefix " + "=" * 16
    print r2_ip_prefix_dict

    r3_route_maps_dict = graph.get_route_maps(r3)
    print "=" * 20 + " r3 route maps " + "=" * 15
    print r3_route_maps_dict

    provider1_route_maps_dict = graph.get_route_maps(provider1)
    print "=" * 20 + " provider1 route maps " + "=" * 8
    print provider1_route_maps_dict

    provider2_route_maps_dict = graph.get_route_maps(provider2)
    print "=" * 20 + " provider2 route maps " + "=" * 8
    print provider2_route_maps_dict

    customer_route_maps_dict = graph.get_route_maps(customer)
    print "=" * 20 + " customer route maps " + "=" * 9
    print customer_route_maps_dict

    ############################### NetComplete ###################################

    external_anns = [ann1, ann2, ann3]
    netcomplete = NetComplete(reqs=reqs, topo=graph, external_announcements=external_anns)
    netcomplete.synthesize()
    netcomplete.write_configs(output_dir=output_dir)

    # added by yongzheng for remind the example finish
    print "=========the example bgp_peers.py finish=========="


if __name__ == '__main__':
    setup_logging()
    parser = argparse.ArgumentParser(description='BGP customer peer example.')
    parser.add_argument('outdir', type=str, help='output directory for the configuration')
    args = parser.parse_args()
    bgp_example(args.outdir)
