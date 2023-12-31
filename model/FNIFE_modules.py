from typing import List

import torch
import torch.nn as nn
import torch.nn.functional as F

from lib.pointops.functions import pointops
from util import pt_util


class _PointNet2SAModuleBase(nn.Module):
    def __init__(self):
        super().__init__()
        self.npoint = None
        self.groupers = None
        self.mlps = None

    def forward(self, xyz: torch.Tensor, features: torch.Tensor = None) -> (torch.Tensor, torch.Tensor):
        r"""
        Parameters
        ----------
        xyz : torch.Tensor
            (B, N, 3) tensor of the xyz coordinates of the features
        features : torch.Tensor
            (B, N, C) tensor of the descriptors of the the features
        Returns
        -------
        new_xyz : torch.Tensor
            (B, npoint, 3) tensor of the new features' xyz
        new_features : torch.Tensor
            (B, npoint, \sum_k(mlps[k][-1])) tensor of the new_features descriptors
        """
        new_features_list = []
        xyz_trans = xyz.transpose(1, 2).contiguous()
        new_xyz = pointops.gathering(
            xyz_trans,
            pointops.furthestsampling(xyz, self.npoint)
        ).transpose(1, 2).contiguous() if self.npoint is not None else None
        for i in range(len(self.groupers)):
            new_features = self.groupers[i](xyz, new_xyz, features)  # (B, C, npoint, nsample)
            new_features = self.mlps[i](new_features)  # (B, mlp[-1], npoint, nsample)
            new_features = F.max_pool2d(new_features, kernel_size=[1, new_features.size(3)])  # (B, mlp[-1], npoint, 1)
            new_features = new_features.squeeze(-1)  # (B, mlp[-1], npoint)
            new_features_list.append(new_features)
        return new_xyz, torch.cat(new_features_list, dim=1)


class PointNet2SAModuleMSG(_PointNet2SAModuleBase):

    def __init__(self, *, npoint: int, radii: List[float], nsamples: List[int], mlps: List[List[int]], bn: bool = True, use_xyz: bool = True):
        super().__init__()
        assert len(radii) == len(nsamples) == len(mlps)
        self.npoint = npoint
        self.groupers = nn.ModuleList()
        self.mlps = nn.ModuleList()
        for i in range(len(radii)):
            radius = radii[i]
            nsample = nsamples[i]
            self.groupers.append(
                pointops.QueryAndGroup(radius, nsample, use_xyz=use_xyz)
                if npoint is not None else pointops.GroupAll(use_xyz)
            )
            mlp_spec = mlps[i]
            if use_xyz:
                mlp_spec[0] += 3
            self.mlps.append(pt_util.SharedMLP(mlp_spec, bn=bn))


class PointNet2SAModule(PointNet2SAModuleMSG):
    def __init__(self, *, mlp: List[int], npoint: int = None, radius: float = None, nsample: int = None, bn: bool = True, use_xyz: bool = True):
        super().__init__(mlps=[mlp], npoint=npoint, radii=[radius], nsamples=[nsample], bn=bn, use_xyz=use_xyz)


class PointNet2FPModule(nn.Module):


    def __init__(self, *, mlp: List[int], bn: bool = True):
        super().__init__()
        self.mlp = pt_util.SharedMLP(mlp, bn=bn)

    def forward(self, unknown: torch.Tensor, known: torch.Tensor, unknow_feats: torch.Tensor, known_feats: torch.Tensor) -> torch.Tensor:
        

        if known is not None:
            dist, idx = pointops.nearestneighbor(unknown, known)
            dist_recip = 1.0 / (dist + 1e-8)
            norm = torch.sum(dist_recip, dim=2, keepdim=True)
            weight = dist_recip / norm
            interpolated_feats = pointops.interpolation(known_feats, idx, weight)
        else:
            interpolated_feats = known_feats.expand(*known_feats.size()[0:2], unknown.size(1))

        if unknow_feats is not None:
            new_features = torch.cat([interpolated_feats, unknow_feats], dim=1)  # (B, C2 + C1, n)
        else:
            new_features = interpolated_feats
        return self.mlp(new_features.unsqueeze(-1)).squeeze(-1)


if __name__ == "__main__":
    torch.manual_seed(1)
    torch.cuda.manual_seed_all(1)
    xyz = torch.randn(2, 9, 3, requires_grad=True).cuda()
    xyz_feats = torch.randn(2, 9, 6, requires_grad=True).cuda()

    test_module = PointNet2SAModuleMSG(npoint=2, radii=[5.0, 10.0], nsamples=[6, 3], mlps=[[9, 3], [9, 6]])
    test_module.cuda()
    print(test_module(xyz, xyz_feats))

    
    for _ in range(1):
        _, new_features = test_module(xyz, xyz_feats)
        new_features.backward(torch.cuda.FloatTensor(*new_features.size()).fill_(1))
        print(new_features)
        print(xyz.grad)
