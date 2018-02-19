#!/usr/bin/env python

"""
    children.py
"""

import torch
from tqdm import tqdm
from workers.helpers import InvalidGraphException

from basenet.helpers import to_numpy

class LoopyDataloader(object):
    """ Wrapper so we can use torchvision loaders in an infinite loop """
    def __init__(self, gen):
        self.batches_per_epoch = len(gen)
        self.epoch_batches     = 0
        self.epochs            = 0
        self.progress          = 0
        
        self._loop = self.__make_loop(gen)
    
    def __make_loop(self, gen):
        while True:
            self.epoch_batches = 0
            for data,target in gen:
                yield data, target
                
                self.progress = self.epochs + (self.epoch_batches / self.batches_per_epoch)
                self.epoch_batches += 1
            
            self.epochs += 1
    
    def __next__(self):
        return next(self._loop)


class Child(object):
    """ Wraps BaseNet model to expose a nice API for ripenet """
    def __init__(self, worker, dataloaders):
        self.worker      = worker
        self.dataloaders = dict([(k, LoopyDataloader(v)) for k,v in dataloaders.items()])
        
        self.train_records = 0
        self.eval_records  = 0
        
    def train_paths(self, paths, n=1):
        loader = self.dataloaders['train']
        for path in paths:
            self.worker.set_path(path)
            if self.worker.is_valid:
                for _ in range(n):
                    data, target = next(loader)
                    self.worker.set_progress(loader.progress)
                    _ = self.worker.train_batch(data, target)
                    
                    self.train_records += data.shape[0]
    
    def eval_paths(self, paths, n=1, mode='val'):
        rewards = []
        
        loader = self.dataloaders[mode]
        for path in paths:
            self.worker.set_path(path)
            if self.worker.is_valid:
                acc = 0
                for _ in range(n):
                    data, target = next(loader)
                    output, _ = self.worker.eval_batch(data, target)
                    acc += (to_numpy(output).argmax(axis=1) == to_numpy(target)).mean()
                    
                    self.eval_records += data.shape[0]
                    
            else:
                acc = -0.1
            
            rewards.append(acc / n)
        
        return torch.FloatTensor(rewards).view(-1, 1)

class LazyChild(Child):
    def train_paths(self, paths):
        pass

