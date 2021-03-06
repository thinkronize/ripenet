#!/usr/bin/env python

"""
    main.py
"""

import os
import re
import sys
import json
import argparse
import numpy as np
from tqdm import tqdm
from collections import OrderedDict

import torch
from torch import nn
from torch.nn import functional as F
from torch.autograd import Variable

from data import make_cifar_dataloaders, make_mnist_dataloaders
from workers import CellWorker

import basenet
from basenet.lr import LRSchedule
from basenet.helpers import to_numpy, set_seeds

np.set_printoptions(linewidth=120)

# --
# CLI

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--outpath', type=str, default='delete-me')
    parser.add_argument('--dataset', type=str, default='cifar10', choices=['cifar10', 'fashion_mnist', 'mnist'])
    parser.add_argument('--architecture', type=str, default='0002_0112')
    
    parser.add_argument('--epochs', type=int, default=50)
    parser.add_argument('--lr-schedule', type=str, default='linear')
    parser.add_argument('--lr-init', type=float, default=0.1)
    
    parser.add_argument('--num-nodes', type=int, default=2) # Number of cells to sample
    
    parser.add_argument('--train-size', type=float, default=0.9)
    parser.add_argument('--pretrained-path', type=str, default=None)
    
    parser.add_argument('--seed', type=int, default=123)
    
    return parser.parse_args()


if __name__ == "__main__":
    
    args = parse_args()
    set_seeds(args.seed)
    
    # --
    # IO
    
    if args.dataset == 'cifar10':
        print('train_cell_worker: make_cifar_dataloaders', file=sys.stderr)
        dataloaders = make_cifar_dataloaders(train_size=args.train_size, download=False, seed=args.seed, pin_memory=True)
    elif 'mnist' in args.dataset:
        print('train_cell_worker: make_mnist_dataloaders (%s)' % args.dataset, file=sys.stderr)
        dataloaders = make_mnist_dataloaders(train_size=args.train_size, download=False, seed=args.seed, pretensor=True, mode=args.dataset)
    else:
        raise Exception()
    
    # --
    # Model
    
    if args.dataset == 'cifar10':
        worker = CellWorker(num_nodes=args.num_nodes).cuda()
    elif 'mnist' in args.dataset:
        # worker = CellWorker(input_channels=1, num_blocks=[1, 1, 1], num_channels=[16, 32, 64], num_nodes=args.num_nodes).cuda()
        # worker = MNISTCellWorker(num_nodes=args.num_nodes).cuda()
        raise Exception()
    else:
        raise Exception()
        
    if args.pretrained_path is not None:
        print('main.py: loading pretrained model %s' % args.pretrained_path, file=sys.stderr)
        worker.load_state_dict(torch.load(args.pretrained_path))
    
    
    # --
    # Training options
    
    lr_scheduler = getattr(LRSchedule, args.lr_schedule)(lr_init=args.lr_init, epochs=args.epochs)
    worker.init_optimizer(
        opt=torch.optim.SGD,
        params=filter(lambda x: x.requires_grad, worker.parameters()),
        lr_scheduler=lr_scheduler,
        momentum=0.9,
        weight_decay=5e-4
    )
    
    # Set architectecture
    architecture = list(re.sub('[^0-9]','', args.architecture))
    architecture = np.array(list(map(int, architecture)))
    assert len(architecture) == (4 * worker.num_nodes), "len(architecture) != 4 * worker.num_nodes"
    print('train_cell_worker: worker.set_path(%s)' % args.architecture, file=sys.stderr)
    worker.set_path(architecture)
    worker.trim_pipes()
    
    print(worker, file=sys.stderr)
    
    cell_pipes = worker.get_pipes()[0]
    config = vars(args)
    print('pipes ->', cell_pipes, file=sys.stderr)
    config['_pipes'] = cell_pipes
    json.dump(config, open(args.outpath + '.config', 'w'))
    
    # --
    # Run
    
    print('train_cell_worker: run', file=sys.stderr)
    
    logfile = open(args.outpath + '.log', 'w')
    
    history = []
    worker.verbose = True
    for epoch in range(args.epochs):
        print('epoch=%d' % epoch, file=sys.stderr)
        train_acc = worker.train_epoch(dataloaders)['acc']
        history.append(OrderedDict([
            ("epoch",     int(epoch)),
            ("train_acc", float(train_acc)),
            ("val_acc",   float(worker.eval_epoch(dataloaders, mode='val')['acc']) if dataloaders['val'] else None),
            ("test_acc",  float(worker.eval_epoch(dataloaders, mode='test')['acc'])),
        ]))
        print(json.dumps(history[-1]), file=logfile)
        logfile.flush()

    worker.save(args.outpath + '.weights')
    logfile.close()
