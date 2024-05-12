#!/usr/bin/python3

import glob
from collections import defaultdict
from collections import namedtuple
from collections import deque
import os
import time
import errno
import sys
import pydot


PM_ENABLE_ADDR = 0x770
HWPREQ_ADDR = 0x774
scaling_govs = glob.glob('/sys/devices/system/cpu/cpufreq/policy*/scaling_governor')
energy_perf_prefs = glob.glob('/sys/devices/system/cpu/cpufreq/policy*/energy_performance_preference')
msr_cpus = glob.glob('/dev/cpu/*/msr')
avail_govs = ['performance', 'powersave']
avail_epp_strings = ['performance', 'balance_performance', 'balance_power', 'power']

HWPRequest = namedtuple('HWPRequest', ['min', 'max', 'des', 'epp', 'window', 'pkg'])
IntelPStateState = namedtuple('IntelPStateState', ['governor', 'epp_string', 'hwpreq'])
ErrorState = namedtuple('ErrorState', [])
Action = namedtuple('Action', ['call', 'name'])
Edge = namedtuple('Edge', ['src', 'action', 'dest'])

def set_val(val, paths):
    failed = False
    for path in paths:
        try:
            with open(path, 'w') as f:
                f.write(val)
        except OSError as e:
            if e.errno == errno.EBUSY:
                failed = True
                break
            else:
                print(e)
                raise
    if not failed:
        maybeval = get_val(paths)
        assert(len(maybeval) == 1 and maybeval[0][0] == val)
    return failed

def set_governor(governor):
    assert(governor in avail_govs)
    return set_val(governor, scaling_govs)

def set_epp_string(epp_string):
    assert(epp_string in avail_epp_strings)
    return set_val(epp_string, energy_perf_prefs)

def get_val(paths):
    occs = defaultdict(int)
    for path in paths:
        with open(path) as f:
            occs[f.read().strip()] += 1
    return sorted(occs.items(), key = lambda x: x[1])

def show_val(occs_sorted):
    print('\n'.join([f'count: {x[1]}\tvalue: {x[0]}' for x in occs_sorted]))
    return None

def show_governor():
    return show_val(get_val(scaling_govs))

def show_epp_string():
    return show_val(get_val(energy_perf_prefs))

def read_msr(addr):
    msr_path = '/dev/cpu/0/msr'
    msr_fd = os.open(msr_path, os.O_RDONLY)
    data = os.pread(msr_fd, 8, addr)
    os.close(msr_fd)
    return int.from_bytes(data, byteorder='little')

def read_hwpreq():
    return read_msr(HWPREQ_ADDR)

def read_pmenable():
    return read_msr(PM_ENABLE_ADDR)

def write_hwpreq(hwpreq):
    val = hwpreq.min & 0xff
    val |= (hwpreq.max & 0xff) << 8
    val |= (hwpreq.des & 0xff) << 16
    val |= (hwpreq.epp & 0xff) << 24
    val |= (hwpreq.window & 0xff3) << 32
    val |= (hwpreq.pkg & 0x1) << 42
    for path in msr_cpus:
        msr_fd = os.open(path, os.O_WRONLY)
        length = 8
        ret = os.pwrite(msr_fd, val.to_bytes(length = length, byteorder = 'little'), HWPREQ_ADDR)
        os.close(msr_fd)
        assert(ret == length)
    return None

def parse_hwpreq(val):
    min_ = (val >> 0) & 0xff
    max_ = (val >> 8) & 0xff
    des = (val >> 16) & 0xff
    epp = (val >> 24) & 0xff
    window = (val >> 32) & 0xff3
    pkg = (val >> 42) & 0x1
    return HWPRequest(min = min_, max = max_, des = des, epp = epp, window = window, pkg = pkg)

def has_hwp():
    with open('/proc/cpuinfo') as f:
        for line in f:
            if line.startswith('flags'):
                flags = line.split(':')[1]
                break
    flags = flags.split()
    return 'hwp' in flags and 'hwp_epp' in flags

def hwp_enabled():
    return read_pmenable()

def actions():
    as_ = []
    # Uber trick, "lambda x=x: f(x)", instead of "lambda: f(x)".
    # The latter would be a closure bound to the latest seen x (the last in the loop).
    # The former uses a default parameter, like "def f(a=2): print(a)"
    for gov in avail_govs:
        as_.append(Action(lambda gov=gov: set_governor(gov), f'set-gov:{gov}'))
    for epp_string in avail_epp_strings:
        as_.append(Action(lambda epp_string=epp_string: set_epp_string(epp_string), f'set-epp-str:{epp_string}'))
    return as_

def get_state():
    maybe_gov = get_val(scaling_govs)
    assert(len(maybe_gov) == 1)
    governor = maybe_gov[0][0]
    maybe_epp = get_val(energy_perf_prefs)
    assert(len(maybe_epp) == 1)
    epp_string = maybe_epp[0][0]
    hwpreq = parse_hwpreq(read_hwpreq())
    return IntelPStateState(governor, epp_string, hwpreq)

def set_state(state):
    set_governor(state.governor)
    set_epp_string(state.epp_string)
    write_hwpreq(state.hwpreq)
    # checking again
    assert(get_state() == state)
    return None

def is_loop(state, action):
    [action_type, action_param] = action.name.split(':')
    loop = False
    if action_type == 'set-gov' and action_param == state.governor:
        loop = True
    elif action_type == 'set-epp-str' and action_param == state.epp_string:
        loop = True
    return loop

def state_label(state):
    if isinstance(state, ErrorState):
        return 'Error'
    label = f'governor = {state.governor}\n'
    label += f'epp_string = {state.epp_string}\n'
    label += f'hwp_request = (min={state.hwpreq.min}, max={state.hwpreq.max}, epp={state.hwpreq.epp})'
    return label

def visit():
    edges = []
    state = get_state()
    seen = set()
    seen.add(state)
    queue = deque()
    queue.appendleft(state)
    while queue:
        state = queue.pop()
        set_state(state)
        for action in actions():
            if is_loop(state, action):
                continue
            failed = action.call()
            if failed:
                newstate = ErrorState()
            else:
                newstate = get_state()
            edges.append(Edge(src = state, action = action.name, dest = newstate))
            print('STATE:', state)
            print('ACTION:', action.name)
            print('NEW STATE:', newstate)
            print()
            if newstate not in seen and not failed:
                seen.add(newstate)
                queue.appendleft(newstate)
            time.sleep(1)
            set_state(state)
    return edges

def addnode(graph, node, ids, id_):
    ids[node] = str(id_)
    graph.add_node(pydot.Node(f'"{id_}"', label = state_label(node)))
    return None

def makedot(edges):
    graph = pydot.Dot('G')
    ids = {}
    id_ = 0
    for edge in edges:
        if edge.src not in ids:
            addnode(graph, edge.src, ids, id_)
            id_ += 1
        if edge.dest not in ids:
            addnode(graph, edge.dest, ids, id_)
            id_ += 1
        graph.add_edge(pydot.Edge(ids[edge.src], ids[edge.dest], label = edge.action))
    return graph

if __name__ == '__main__':
    if not has_hwp() or not hwp_enabled():
        print('Error: HWP not present, or not enabled', file=sys.stderr)
        sys.exit(1)
    initstate = get_state()
    edges = visit()
    set_state(initstate)
    graph = makedot(edges)
    fname = 'intel-pstate-states.dot'
    graph.write_dot(fname)
    print(f'Written {fname}')
