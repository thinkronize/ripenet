#!/usr/bin/env python

"""
    action-plot.py
"""

import sys
import pandas as pd
import numpy as np

from rsub import *
from matplotlib import pyplot as plt
plt.rcParams['image.cmap'] = 'rainbow'

# --

# f = sys.argv[1]
f = '_results/hyperband/hyperband.resample_5/test.actions'

df = pd.read_csv(f, header=None, sep='\t')
df.columns = ['mode', 'epoch', 'score'] + range(df.shape[1] - 4) + ['aid']


dfs = df[df.epoch > 500]
sel = '[0 0 5 5 0 1 3 3 0 1 4 2]'
for idx, i in enumerate(np.unique(dfs.aid)):
    print(i)
    tmp = dfs[dfs.aid == i]
    tmp.score = tmp.score.rolling(10).mean()
    c = 'red' if tmp.aid.iloc[0] == sel else 'grey'
    _ = plt.plot(tmp.epoch, tmp.score, alpha=0.5, label=i, c=c)

_ = plt.ylim(0.85, 1)
_ = plt.xlim(650, 730)

_ = plt.grid(alpha=0.25)
_ = plt.yticks(list(plt.yticks()[0]) + list(np.arange(0.9, 1.0, 0.01)))
show_plot()

# df.loc[df[df.epoch == 719].score.argmax()]
df[(df.epoch > 715) & (df.epoch < 720)].groupby('aid').score.mean().sort_values()



# --

N = 1000
g50 = df.groupby('epoch').score.quantile(0.50)
m50 = df.groupby('epoch').score.quantile(0.50).cummax()
g75 = df.groupby('epoch').score.quantile(0.75)
m75 = df.groupby('epoch').score.quantile(0.75).cummax()
# m95 = df.groupby('epoch').score.quantile(0.95).cummax()
# m100 = df.groupby('epoch').score.quantile(1.0).cummax()

_ = plt.plot(g50.tail(N))
_ = plt.plot(m50.tail(N))
_ = plt.plot(g75.tail(N))
_ = plt.plot(m75.tail(N))

# _ = plt.plot(m95.tail(N))
# _ = plt.plot(m100.tail(N))
_ = plt.ylim(0.85, 1)
# _ = plt.xlim(300, 370)

_ = plt.grid(alpha=0.25)
_ = plt.yticks(list(plt.yticks()[0]) + list(np.arange(0.9, 1.0, 0.01)))
show_plot()


