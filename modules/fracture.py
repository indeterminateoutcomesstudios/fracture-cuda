# -*- coding: utf-8 -*-

from collections import defaultdict
from numpy import pi as PI
from numpy import zeros
from numpy import ones
from numpy import sin
from numpy import cos
from numpy import arctan2
from numpy import arange
from numpy import column_stack
from numpy import row_stack
from numpy.random import random
from numpy.random import randint


from numpy import float32 as npfloat
from numpy import int32 as npint

import pycuda.driver as drv

TWOPI = PI*2
HPI = PI*0.5


class Fracture(object):
  def __init__(
      self,
      frac_dot,
      frac_dst,
      frac_stp,
      initial_sources,
      frac_spd=1.0,
      frac_diminish=1.0,
      frac_spawn_diminish=1.0,
      threads = 256,
      zone_leap = 1024,
      nmax = 100000
      ):
    self.itt = 0

    self.frac_dot = frac_dot
    self.frac_dst = frac_dst
    self.frac_stp = frac_stp
    self.frac_spd = frac_spd

    self.frac_diminish = frac_diminish
    self.spawn_diminish = frac_spawn_diminish

    self.threads = threads
    self.zone_leap = zone_leap

    self.nmax = nmax

    self.num = 0
    self.fnum = 0
    self.anum = 0

    self.__init(initial_sources)
    self.__cuda_init()

  def __init(self, initial_sources):
    num = len(initial_sources)
    self.num = num

    nz = int(0.5/self.frac_dst)
    self.nz = nz
    print('nz', nz)
    self.nz2 = nz**2
    nmax = self.nmax

    self.xy = zeros((nmax, 2), npfloat)
    self.xy[:num,:] = initial_sources[:,:]

    self.fid_node = zeros((nmax, 2), npint)
    self.fid_node[:,:] = -1

    self.visited = zeros((nmax, 1), npint)
    self.visited[:, :] = -1
    self.active = zeros((nmax, 1), npint)
    self.active[:, :] = -1

    self.diminish = ones((nmax, 1), npfloat)
    self.spd = ones((nmax, 1), npfloat)
    self.dxy = ones((nmax, 2), npfloat)
    self.ndxy = ones((nmax, 2), npfloat)
    self.cand_ndxy = ones((nmax, 2), npfloat)

    self.tmp = ones((nmax, 2), npfloat)

    self.zone_num = zeros(self.nz2, npint)
    self.zone_node = zeros(self.nz2*self.zone_leap, npint)

  def __cuda_init(self):
    import pycuda.autoinit
    from .helpers import load_kernel

    self.cuda_agg = load_kernel(
        'modules/cuda/agg.cu',
        'agg',
        subs={'_THREADS_': self.threads}
        )
    self.cuda_step = load_kernel(
        'modules/cuda/calc_stp.cu',
        'calc_stp',
        subs={
          '_THREADS_': self.threads,
          '_PROX_': self.zone_leap
          }
        )

  def _add_nodes(self, xy):
    num = self.num
    n, _ = xy.shape
    inds = arange(num, num+n)
    self.xy[inds, :] = xy
    self.num += len(xy)
    return inds

  def _add_fracs(self, dxy, nodes, fids=None, replace_active=False):
    fnum = self.fnum
    n, _ = dxy.shape

    new_fracs = arange(fnum, fnum+n)

    if len(nodes)==1 and n>1:
      nodes = ones(n, 'int')
      nodes[:] = nodes[0]

    if fids is None:
      fids = new_fracs

    fid_node = column_stack((
        fids,
        nodes
        ))

    self.dxy[new_fracs, :] = dxy
    self.spd[new_fracs, :] = self.frac_spd
    self.fid_node[new_fracs, :] = fid_node
    self.visited[new_fracs, 0] = 1

    if not replace_active:
      self.active[self.anum:self.anum+n, 0] = new_fracs
      self.anum += n
    else:
      self.active[:n, 0] = new_fracs
      self.anum = n

    self.fnum += n
    return new_fracs

  def _do_steps(self, active, ndxy):
    mask = ndxy[:, 0] >= -1.0
    n = mask.sum()
    if n<1:
      return False

    ndxy = ndxy[mask, :]
    active = active[mask, 0]
    ii = self.fid_node[active, 1].squeeze()
    new_xy = self.xy[ii, :] + ndxy*self.frac_stp
    new_nodes = self._add_nodes(new_xy)

    self._add_fracs(
        ndxy,
        new_nodes,
        self.fid_node[active, 0],
        replace_active=True
        )

    return True

  def get_nodes(self):
    return self.xy[:self.num, :]

  def get_fractures(self):
    res = defaultdict(list)

    for fid, node in self.fid_node[:self.fnum, :]:
      res[fid].append(self.xy[node, :])

    return [row_stack(v) for k, v in res.items()]

  def get_fractures_inds(self):
    res = defaultdict(list)

    for fid, node in self.fid_node[:self.fnum, :]:
      res[fid].append(node)

    return [v for k, v in res.items()]

  def blow(self, n, xy):
    a = random(size=n)*TWOPI
    dxy = column_stack((
        cos(a),
        sin(a)
        ))

    new_nodes = self._add_nodes(xy)
    self._add_fracs(dxy, new_nodes)

  def spawn_front(self, factor, angle):
    inds = (random(self.anum)<factor).nonzero()[0]
    n = len(inds)
    if n<1:
      return 0

    cand_ii = self.fid_node[self.active[inds, 0], 1]

    num = self.num
    fnum = self.fnum

    xy = self.xy[:num, :]
    new = arange(fnum, fnum+n)
    orig_dxy = self.dxy[cand_ii, :]
    rndtheta = (-1)**randint(2, size=n)*HPI + (0.5-random(n)) * angle
    theta = arctan2(orig_dxy[:, 1], orig_dxy[:, 0]) + rndtheta

    fid_node = column_stack((
        new,
        cand_ii
        ))
    cand_dxy = column_stack((
        cos(theta),
        sin(theta)
        ))
    nactive = arange(n)

    ndxy = self.cand_ndxy[:n, :]

    self.cuda_step(
        npint(self.nz),
        npint(self.zone_leap),
        npint(num),
        npint(n),
        npint(n),
        npfloat(self.frac_dot),
        npfloat(self.frac_dst),
        npfloat(self.frac_stp),
        drv.In(fid_node),
        drv.In(nactive),
        drv.Out(self.tmp[:n, :]),
        drv.In(xy),

        drv.In(cand_dxy),
        drv.Out(ndxy),

        drv.In(self.zone_num),
        drv.In(self.zone_node),
        block=(self.threads,1,1),
        grid=(int(n//self.threads + 1), 1) # this cant be a numpy int for some reason
        )

    mask = ndxy[:, 0] >= -1.0
    n = mask.sum()

    if n<1:
      return False

    print('new', n, self.anum)
    self._add_fracs(ndxy[mask, :], cand_ii[mask])
    return True

  def step(self):
    self.itt += 1

    num = self.num
    fnum = self.fnum
    anum = self.anum

    xy = self.xy[:num, :]
    active = self.active[:anum]
    fid_node = self.fid_node[:fnum]
    dxy = self.dxy[:fnum, :]
    ndxy = self.ndxy[:anum, :]

    tmp = self.tmp[:anum, :]

    self.zone_num[:] = 0

    self.cuda_agg(
        npint(self.nz),
        npint(self.zone_leap),
        npint(num),
        drv.In(xy),
        drv.InOut(self.zone_num),
        drv.InOut(self.zone_node),
        block=(self.threads,1,1),
        grid=(int(num//self.threads + 1), 1) # this cant be a numpy int for some reason
        )

    ndxy[:,:] = -10
    self.cuda_step(
        npint(self.nz),
        npint(self.zone_leap),
        npint(num),
        npint(fnum),
        npint(anum),
        npfloat(self.frac_dot),
        npfloat(self.frac_dst),
        npfloat(self.frac_stp),
        drv.In(fid_node),
        drv.In(active),
        drv.Out(tmp),
        drv.In(xy),
        drv.In(dxy),
        drv.Out(ndxy),
        drv.In(self.zone_num),
        drv.In(self.zone_node),
        block=(self.threads,1,1),
        grid=(int(anum//self.threads + 1), 1) # this cant be a numpy int for some reason
        )

    # print('active\n', active, '\n')
    # print('fid_node\n', fid_node, '\n')
    # print('ndxy\n', ndxy, '\n')
    # print('tmp\n', tmp, '\n')

    res = self._do_steps(active, ndxy)

    return res


