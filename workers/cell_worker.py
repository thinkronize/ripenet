#!/usr/bin/env python

"""
    pipenet.py
"""

from __future__ import print_function, division

import sys
import itertools
import numpy as np
from dask import get
from dask.optimize import cull
from pprint import pprint
from collections import OrderedDict
from functools import partial

import torch
from torch import nn
from torch.nn import functional as F
from torch.autograd import Variable

import basenet
from basenet.helpers import to_numpy

from .helpers import InvalidGraphException

# --
# Helper layers

class Accumulator(nn.Module):
    def __init__(self, agg_fn=torch.sum, name='noname'):
        super(Accumulator, self).__init__()
        
        self.agg_fn = agg_fn
        self.name = name
    
    def forward(self, parts):
        parts = [part for part in parts if part is not None]
        if len(parts) == 0:
            return None
        else:
            return self.agg_fn(torch.stack(parts), dim=0)
    
    def __repr__(self):
        return 'Accumulator(%s)' % self.name


class Flatten(nn.Module):
    def forward(self, x):
        return x.view(x.shape[0], -1)
        
    def __repr__(self):
        return "Flatten()"


class IdentityLayer(nn.Module):
    def __init__(self, in_channels, out_channels, stride=1):
        super(IdentityLayer, self).__init__()
        
        self.in_channels = in_channels
        self.out_channels = out_channels
        
        if (in_channels != out_channels) or (stride != 1):
            self.bn = nn.BatchNorm2d(in_channels, track_running_stats=False)
            self.conv = nn.Conv2d(in_channels=in_channels, out_channels=out_channels, stride=stride, kernel_size=stride)
        else:
            self.conv = None
    
    def forward(self, x):
        if self.conv is not None:
            return self.conv(self.bn(x))
        else:
            return x
    
    def __repr__(self):
        return "IdentityLayer(%d -> %d)" % (self.in_channels, self.out_channels)


class NoopLayer(nn.Module):
    def __init__(self, in_channels, out_channels, stride=1):
        super(NoopLayer, self).__init__()
        
        self.in_channels  = in_channels
        self.out_channels = out_channels
        self.stride       = stride
    
    def forward(self, x):
        out = Variable(torch.zeros(x.shape[0], self.out_channels, x.shape[2] / self.stride, x.shape[3] / self.stride))
        if x.is_cuda:
            out = out.cuda()
        
        return out
    
    def __repr__(self):
        return "NoopLayer(%d -> %d | stride=%d)" % (self.in_channels, self.out_channels, self.stride)


class BNConv2d(nn.Module):
    def __init__(self, in_channels, out_channels, **kwargs):
        super(BNConv2d, self).__init__()
        
        self.add_module('bn', nn.BatchNorm2d(in_channels,track_running_stats=False))
        self.add_module('relu', nn.ReLU())
        self.add_module('conv', nn.Conv2d(in_channels, out_channels, **kwargs))
    
    def forward(self, x):
        return self.conv(self.relu(self.bn(x)))
    
    def __repr__(self):
        return 'BN' + self.conv.__repr__()


class ReshapePool2d(nn.Module):
    def __init__(self, in_channels, out_channels, mode='avg', **kwargs):
        super(ReshapePool2d, self).__init__()
        
        self.in_channels  = in_channels
        self.out_channels = out_channels
        
        if in_channels != out_channels:
            self.conv = nn.Conv2d(in_channels=in_channels, out_channels=out_channels, kernel_size=1)
        else:
            self.conv = None
        
        if mode == 'avg':
            self.pool = nn.AvgPool2d(**kwargs)
        else:
            self.pool = nn.MaxPool2d(**kwargs)
    
    def forward(self, x):
        if self.conv is not None:
            x = self.conv(x)
        
        return self.pool(x)


class BNSepConv2d(BNConv2d):
    def __init__(self, **kwargs):
        assert 'groups' not in kwargs, "BNSepConv2d: cannot specify groups"
        kwargs['groups'] = min(kwargs['in_channels'], kwargs['out_channels'])
        super(BNSepConv2d, self).__init__(**kwargs)
    
    def __repr__(self):
        return 'BNSep' + self.conv.__repr__()


# --
# Blocks

class CellBlock(nn.Module):
    def __init__(self, in_channels, out_channels, stride=1, num_nodes=2, num_branches=2):
        super(CellBlock, self).__init__()
        
        self.num_nodes = num_nodes
        
        self.op_fns = OrderedDict([
            ("noop____", NoopLayer),
            ("identity", IdentityLayer),
            ("conv3___", partial(BNConv2d, kernel_size=3, padding=1)),
            ("conv5___", partial(BNConv2d, stride=stride, kernel_size=5, padding=2)),
            ("sepconv3", partial(BNSepConv2d, stride=stride, kernel_size=3, padding=1)),
            ("sepconv5", partial(BNSepConv2d, stride=stride, kernel_size=5, padding=2)),
            ("avgpool_", partial(ReshapePool2d, mode='avg', stride=stride, kernel_size=3, padding=1)),
            ("maxpool_", partial(ReshapePool2d, mode='max', stride=stride, kernel_size=3, padding=1)),
        ])
        self.op_lookup = dict(zip(range(len(self.op_fns)), self.op_fns.keys()))
        
        # --
        # Create nodes (just sum accumulators)
        
        self.nodes = OrderedDict([])
        for node_id in range(num_nodes):
            self.nodes['node_%d' % node_id] = Accumulator(name="node_%d" % node_id)
        
        for k, v in self.nodes.items():
            self.add_module(str(k), v)
        
        # --
        # Create pipes
        
        self.pipes = OrderedDict([])
        
        # Add pipes from input data to all nodes
        for trg_id in range(num_nodes):
            for src_id in ['data_0']:
                for branch in range(num_branches):
                    for op_key, op_fn in self.op_fns.items():
                        self.pipes[(src_id, 'node_%d' % trg_id, op_key, branch)] = op_fn(in_channels=in_channels, out_channels=out_channels, stride=stride)
        
        # Add pipes between all nodes
        for trg_id in range(num_nodes):
            for src_id in range(trg_id):
                for branch in range(num_branches):
                    for op_key, op_fn in self.op_fns.items():
                        self.pipes[('node_%d' % src_id, 'node_%d' % trg_id, op_key, branch)] = op_fn(in_channels=out_channels, out_channels=out_channels, stride=1)
        
        for k, v in self.pipes.items():
            self.add_module(str(k), v)
        
        # --
        # Set default architecture
        
        # 0002|0112
        self._default_pipes = [
            ('data_0', 'node_0', 'noop____', 0),
            ('data_0', 'node_0', 'conv3___', 1),
            ('data_0', 'node_1', 'identity', 0),
            ('node_0', 'node_1', 'conv3___', 1),
        ]
        self.reset_pipes()
    
    def reset_pipes(self):
        self.set_pipes(self._default_pipes)
    
    def get_pipes(self):
        return [pipe_name for pipe_name, pipe in self.pipes.items() if pipe.active]
    
    def get_pipes_mask(self):
        return [pipe.active for pipe in self.pipes.values()]
    
    def set_pipes(self, pipes):
        self.active_pipes = [tuple(pipe) for pipe in pipes]
        
        for pipe_name, pipe in self.pipes.items():
            pipe.active = pipe_name in self.active_pipes
        
        # --
        # Add cells to graph
        
        self.graph = OrderedDict([('data_0', None)])
        for node_name, node in self.nodes.items():
            node_inputs = [pipe_name for pipe_name, pipe in self.pipes.items() if (pipe_name[1] == node_name) and (pipe.active)]
            self.graph[node_name] = (node, node_inputs)
        
        # --
        # Add pipes to graph
        
        for pipe_name, pipe in self.pipes.items():
            if pipe.active:
                self.graph[pipe_name] = (pipe, pipe_name[0])
        
        # --
        # Gather loose ends for output
        
        nodes_w_output  = set([k[0] for k in self.graph.keys() if isinstance(k, tuple)])
        nodes_wo_output = [k for k in self.graph.keys() if ('node' in k) and (k not in nodes_w_output)]
        
        self.graph['_output'] = (Accumulator(name='_output', agg_fn=torch.mean), nodes_wo_output) # sum or avg or concat?  which is best?
    
    def set_path(self, path):
        path = to_numpy(path).reshape(-1, 4)
        pipes = []
        for i, path_block in enumerate(path):
            trg_id = 'node_%d' % i # !! Indexing here is a little confusing
            
            src_0 = 'node_%d' % (path_block[0] - 1) if path_block[0] != 0 else "data_0" # !! Indexing here is a little confusing
            src_1 = 'node_%d' % (path_block[1] - 1) if path_block[1] != 0 else "data_0" # !! Indexing here is a little confusing
            
            pipes += [
                (src_0, trg_id, self.op_lookup[path_block[2]], 0),
                (src_1, trg_id, self.op_lookup[path_block[3]], 1),
            ]
        
        for pipe in pipes:
            assert pipe in self.pipes.keys()
        
        self.set_pipes(pipes)
    
    def trim_pipes(self):
        for k,v in self.pipes.items():
            if k not in self.active_pipes:
                delattr(self, str(k))
    
    @property
    def is_valid(self, layer='_output'):
        return 'data_0' in cull(self.graph, layer)[0]
    
    def forward(self, x, layer='_output'):
        if not self.is_valid:
            raise InvalidGraphException
        
        self.graph['data_0'] = x
        out = get(self.graph, layer)
        self.graph['data_0'] = None
        
        if out is None:
            raise InvalidGraphException
        
        return out

# --
# Models

class _CellWorker(basenet.BaseNet):
    def reset_pipes(self):
        for cell_block in self.cell_blocks:
            _ = cell_block.reset_pipes()
    
    def get_pipes(self):
        tmp = []
        for cell_block in self.cell_blocks:
            tmp.append(cell_block.get_pipes())
        
        return tmp
    
    def get_pipes_mask(self):
        tmp = []
        for cell_block in self.cell_blocks:
            tmp.append(cell_block.get_pipes_mask())
        
        return tmp
    
    def set_pipes(self, pipes):
        for cell_block in self.cell_blocks:
            _ = cell_block.set_pipes(pipes)
    
    def set_path(self, path):
        for cell_block in self.cell_blocks:
            _ = cell_block.set_path(path)
    
    def trim_pipes(self):
        for cell_block in self.cell_blocks:
            _ = cell_block.trim_pipes()
    
    @property
    def is_valid(self, layer='_output'):
        return np.all([cell_block.is_valid for cell_block in self.cell_blocks])


AVG_CLASSIFIER = False
class CellWorker(_CellWorker):
    
    def __init__(self, num_classes=10, input_channels=3, num_blocks=[2, 2, 2, 2], num_channels=[32, 64, 128, 256, 512], num_nodes=2):
        super(CellWorker, self).__init__()
        
        self.num_nodes = num_nodes
        
        self.prep = nn.Conv2d(in_channels=input_channels, out_channels=num_channels[0], kernel_size=3, padding=1)
        
        self.cell_blocks = []
        
        all_layers = []
        for i, (block, in_channels, out_channels) in enumerate(zip(num_blocks, num_channels[:-1], num_channels[1:])):
            layers = []
            
            # Add cell at beginning that changes num channels
            cell_block = CellBlock(in_channels=in_channels, out_channels=out_channels, num_nodes=num_nodes, stride=2 if i > 0 else 1)
            layers.append(cell_block)
            self.cell_blocks.append(cell_block)
            
            # Add cells that preserve channels
            for _ in range(block - 1):
                cell_block = CellBlock(in_channels=out_channels, out_channels=out_channels, num_nodes=num_nodes , stride=1)
                layers.append(cell_block)
                self.cell_blocks.append(cell_block)
            
            all_layers.append(nn.Sequential(*layers))
        
        self.layers = nn.Sequential(*all_layers)
        if AVG_CLASSIFIER:
            self.classifier = nn.Sequential(
                BNConv2d(in_channels=num_channels[-1], out_channels=num_classes, kernel_size=1, padding=0, stride=1),
                nn.AdaptiveAvgPool2d((1, 1)),
            )
        else:
            self.linear = nn.Linear(num_channels[-1], num_classes)
    
    def forward(self, x):
        x = self.prep(x)
        x = self.layers(x)
        
        if AVG_CLASSIFIER:
            x = self.classifier(x)
            x = x.view((x.shape[0], x.shape[1]))
        else:
            x = F.adaptive_avg_pool2d(x, (1, 1))
            x = x.view(x.size(0), -1)
            x = self.linear(x)
        
        return x

# >>
# MNIST

class MNISTCellWorker(_CellWorker):
    def __init__(self, num_classes=10, input_channels=1, channels=64, num_nodes=2, num_branches=2):
        super(MNISTCellWorker, self).__init__()
        
        self.num_nodes = num_nodes
        
        self.prep = nn.Sequential(*[
            nn.Conv2d(1, 32, kernel_size=3, padding=1),
            nn.MaxPool2d(2),
            nn.ReLU(),
        ])
        
        # self.cell_block = CellBlock(channels=64, num_nodes=num_nodes, num_branches=num_branches)
        self.cell_block = nn.Conv2d(32, 64, kernel_size=5, padding=2)
        self.cell_blocks = []
        
        self.post = nn.Sequential(*[
            nn.Dropout2d(p=0.5),
            nn.MaxPool2d(2),
            nn.ReLU(),
            Flatten(),
            nn.Linear(3136, 128),
            nn.ReLU(),
            nn.Dropout(p=0.5),
            nn.Linear(128, 10),
        ])
    
    def forward(self, x):
        x = self.prep(x)
        x = self.cell_block(x)
        x = self.post(x)
        return x

# >>
# FTOP

class FTopNode(nn.Module):
    def __init__(self, op_fns):
        super(FTopNode, self).__init__()
        
        self.nodes      = nn.ModuleList([f() for f in op_fns.values()])
        self.node_names = op_fns.keys()
        self.idx        = 0
    
    def set_idx(self, idx):
        self.idx = idx
    
    def forward(self, x):
        return self.nodes[self.idx](x)
    
    def __repr__(self):
        return self.nodes[self.idx].__repr__()


class FTopBlock(nn.Module):
    def __init__(self, channels, stride=1, num_nodes=2, num_branches=2):
        super(FTopBlock, self).__init__()
        
        self.num_nodes    = num_nodes
        self.num_branches = num_branches
        
        self.op_fns = OrderedDict([
            ("noop____", NoopLayer),
            ("identity", IdentityLayer),
            ("conv3___", partial(BNConv2d, in_channels=channels, out_channels=channels, stride=stride, kernel_size=3, padding=1)),
            ("conv5___", partial(BNConv2d, in_channels=channels, out_channels=channels, stride=stride, kernel_size=5, padding=2)),
            ("sepconv3", partial(BNSepConv2d, in_channels=channels, out_channels=channels, stride=stride, kernel_size=3, padding=1)),
            ("sepconv5", partial(BNSepConv2d, in_channels=channels, out_channels=channels, stride=stride, kernel_size=5, padding=2)),
            ("avgpool_", partial(nn.AvgPool2d, stride=stride, kernel_size=3, padding=1)),
            ("maxpool_", partial(nn.MaxPool2d, stride=stride, kernel_size=3, padding=1)),
        ])
        self.op_lookup = dict(zip(range(len(self.op_fns)), self.op_fns.keys()))
        
        self.branches = []
        for branch_id in range(num_branches):
            branch = []
            for node_id in range(num_nodes):
                node = FTopNode(self.op_fns)
                branch.append(node)
            
            self.branches.append(nn.Sequential(*branch))
        
        self.branches = nn.ModuleList(self.branches)
        
        # --
        # Set default architecture
        
        self._default_pipes = np.array([1, 1, 1, 1])
        self.reset_pipes()
    
    def reset_pipes(self):
        self.set_path(self._default_pipes)
    
    def get_pipes(self):
        return list(map(int, self.pipes))
    
    def set_path(self, path):
        self.pipes = to_numpy(path).astype(int)
        
        path = to_numpy(path).reshape(-1, self.num_branches)
        for branch_id, branch_path in enumerate(path):
            for node_id, b in enumerate(branch_path):
                self.branches[branch_id][node_id].idx = b
        
    def forward(self, x, agg_fn=torch.sum):
        res = [branch(x) for branch in self.branches]
        return agg_fn(torch.stack(res), dim=0)


class FTopWorker(_CellWorker):
    def __init__(self, num_classes=10, input_channels=1, channels=64, num_nodes=2, num_branches=2):
        super(FTopWorker, self).__init__()
        
        self.num_nodes = num_nodes
        
        self.prep = nn.Sequential(*[
            nn.Conv2d(1, 64, kernel_size=3, padding=1),
            nn.MaxPool2d(2),
            nn.ReLU(),
        ])
        
        self.cell_block = FTopBlock(channels=64, num_nodes=num_nodes, num_branches=num_branches)
        self.cell_blocks = [self.cell_block]
        
        self.post = nn.Sequential(*[
            nn.Dropout2d(p=0.5),
            nn.MaxPool2d(2),
            nn.ReLU(),
            Flatten(),
            nn.Linear(3136, 128),
            nn.ReLU(),
            nn.Dropout(p=0.5),
            nn.Linear(128, 10),
        ])
    
    def forward(self, x):
        x = self.prep(x)
        x = self.cell_block(x)
        x = self.post(x)
        return x
    
    @property
    def is_valid(self):
        return True

# <<