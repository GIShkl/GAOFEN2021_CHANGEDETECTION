# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

"""ChangeStar implementations."""

from typing import Dict, List

import torch
import torch.nn as nn
from einops import rearrange
from torch import Tensor
from torch.nn.modules import Module
from .farseg import FarSeg
from models.backbone.hrnet import HRNet
# https://github.com/pytorch/pytorch/issues/60979
# https://github.com/pytorch/pytorch/pull/61045
Module.__module__ = "torch.nn"


class ChangeMixin(Module):
    """This module enables any segmentation model to detect binary change.

    The common usage is to attach this module on a segmentation model without the
    classification head.

    If you use this model in your research, please cite the following paper:

    * https://arxiv.org/abs/2108.07002
    """

    def __init__(
        self,
        in_channels: int = 128 * 2,
        inner_channels: int = 16,
        num_convs: int = 4,
        scale_factor: float = 4.0,
    ):
        """Initializes a new ChangeMixin module.

        Args:
            in_channels: sum of channels of bitemporal feature maps
            inner_channels: number of channels of inner feature maps
            num_convs: number of convolution blocks
            scale_factor: number of upsampling factor
        """
        super().__init__()  # type: ignore[no-untyped-call]
        layers: List[Module] = [
            nn.modules.Sequential(
                nn.modules.Conv2d(in_channels, inner_channels, 3, 1, 1),
                # type: ignore[no-untyped-call]
                nn.modules.BatchNorm2d(inner_channels),
                nn.modules.ReLU(True),
            )
        ]
        layers += [
            nn.modules.Sequential(
                nn.modules.Conv2d(inner_channels, inner_channels, 3, 1, 1),
                # type: ignore[no-untyped-call]
                nn.modules.BatchNorm2d(inner_channels),
                nn.modules.ReLU(True),
            )
            for _ in range(num_convs - 1)
        ]

        cls_layer = nn.modules.Conv2d(inner_channels, 1, 3, 1, 1)

        layers.append(cls_layer)
        layers.append(nn.modules.UpsamplingBilinear2d(
            scale_factor=scale_factor))

        self.convs = nn.modules.Sequential(*layers)

    def forward(self, bi_feature: Tensor) -> List[Tensor]:
        """Forward pass of the model.

        Args:
            x: input bitemporal feature maps of shape [b, t, c, h, w]

        Returns:
            a list of bidirected output predictions
        """
        batch_size = bi_feature.size(0)
        # *********************temporal swap module (TSM) **********************
        # 合并
        t1t2 = torch.cat(  # type: ignore[attr-defined]
            [bi_feature[:, 0, :, :, :], bi_feature[:, 1, :, :, :]], dim=1
        )
        # 合并
        t2t1 = torch.cat(  # type: ignore[attr-defined]
            [bi_feature[:, 1, :, :, :], bi_feature[:, 0, :, :, :]], dim=1
        )

        # ********************a small FCN composed of N 3×3 conv layers******************
        # type: ignore[attr-defined]
        c1221 = self.convs(torch.cat([t1t2, t2t1], dim=0))
        # 双向输出
        # type: ignore[no-untyped-call]
        c12, c21 = torch.split(c1221, batch_size, dim=0)
        return [c12, c21]


class ChangeStar(Module):
    """The base class of the network architecture of ChangeStar.

    ChangeStar is composed of an any segmentation model and a ChangeMixin module.
    This model is mainly used for binary/multi-class change detection under bitemporal
    supervision and single-temporal supervision. It features the property of
    segmentation architecture reusing, which is helpful to integrate advanced dense
    prediction (e.g., semantic segmentation) network architecture into change detection.

    For multi-class change detection, semantic change prediction can be inferred by a
    binary change prediction from the ChangeMixin module and two semantic predictions
    from the Segmentation model.

    If you use this model in your research, please cite the following paper:

    * https://arxiv.org/abs/2108.07002
    """

    def __init__(
        self,
        dense_feature_extractor: Module,
        seg_classifier: Module,
        changemixin: ChangeMixin,
        inference_mode: str = "t1t2",
    ) -> None:
        """Initializes a new ChangeStar model.

        Args:
            dense_feature_extractor: module for dense feature extraction, typically a
                semantic segmentation model without semantic segmentation head.
            seg_classifier: semantic segmentation head, typically a convolutional layer
                followed by an upsampling layer.
            changemixin: :class:`torchgeo.models.ChangeMixin` module
            inference_mode: name of inference mode ``'t1t2'`` | ``'t2t1'`` | ``'mean'``.
                ``'t1t2'``: concatenate bitemporal features in the order of t1->t2;
                ``'t2t1'``: concatenate bitemporal features in the order of t2->t1;
                ``'mean'``: the weighted mean of the output of ``'t1t2'`` and ``'t1t2'``
        """
        super().__init__()  # type: ignore[no-untyped-call]
        self.dense_feature_extractor = dense_feature_extractor
        self.seg_classifier = seg_classifier
        self.changemixin = changemixin

        if inference_mode not in ["t1t2", "t2t1", "mean"]:
            raise ValueError(f"Unknown inference_mode: {inference_mode}")
        self.inference_mode = inference_mode

    def forward(self, x: Tensor) -> Dict[str, Tensor]:
        """Forward pass of the model.

        Args:
            x: a bitemporal input tensor of shape [B, T, C, H, W]

        Returns:
            a dictionary containing
                if training stage, returning
                    bi_seg_logit: bitemporal semantic segmentation logit
                    bi_change_logit: bidirected binary change detection logit
                if inference stage, returning
                    bi_seg_logit: bitemporal semantic segmentation logit
                    change_prob:  binary change detection probability
        """
        # print(x.shape) # torch.Size([2, 2, 3, 512, 512])
        b, t, c, h, w = x.shape
        # 组合双时相输入
        x = rearrange(x, "b t c h w -> (b t) c h w")
        # print(x.shape) # torch.Size([4, 3, 512, 512])
        # feature extraction 提取双时相特征
        bi_feature = self.dense_feature_extractor(x)
        # semantic segmentation
        # bi_seg_logit = self.seg_classifier(bi_feature)
        # #
        # bi_seg_logit = rearrange(bi_seg_logit, "(b t) c h w -> b t c h w", t=t)
        #
        bi_feature = rearrange(bi_feature, "(b t) c h w -> b t c h w", t=t)
        # bidirected change detection
        c12, c21 = self.changemixin(bi_feature)

        results: Dict[str, Tensor] = {}

        # 预测输出
        if not self.training:
            # results.update({"bi_seg_logit": bi_seg_logit})
            #
            if self.inference_mode == "t1t2":
                results.update({"change_prob": c12.sigmoid()})
            elif self.inference_mode == "t2t1":
                results.update({"change_prob": c21.sigmoid()})
            elif self.inference_mode == "mean":
                results.update(
                    {
                        "change_prob": torch.stack([c12, c21], dim=0).sigmoid_().mean(dim=0)
                    }
                )
        else:
            results.update(
                {
                    # "bi_seg_logit": bi_seg_logit,
                    "bi_change_logit": torch.stack([c12, c21], dim=1),
                }
            )
        return results


class ChangeStarFarSeg(ChangeStar):
    """The network architecture of ChangeStar(FarSeg).

    ChangeStar(FarSeg) is composed of a FarSeg model and a ChangeMixin module.

    If you use this model in your research, please cite the following paper:

    * https://arxiv.org/abs/2108.07002
    """

    def __init__(
        self,
        backbone: str = "resnet50",
        classes: int = 1,
        backbone_pretrained: bool = True,
    ) -> None:
        """Initializes a new ChangeStarFarSeg model.

        Args:
            backbone: name of ResNet backbone
            classes: number of output segmentation classes
            backbone_pretrained: whether to use pretrained weight for backbone
        """
        # segmentation model
        model = FarSeg(backbone=backbone, classes=classes,
                       backbone_pretrained=backbone_pretrained)
        # head
        seg_classifier: Module = model.decoder.classifier
        #
        model.decoder.classifier = (
            nn.modules.Identity()  # type: ignore[no-untyped-call, assignment]
        )
        # 调用父类的构造函数
        super().__init__(
            dense_feature_extractor=model,  # extract feature
            seg_classifier=seg_classifier,  # classification head
            # change detection module
            changemixin=ChangeMixin(
                in_channels=128 * 2, inner_channels=16, num_convs=4, scale_factor=4.0
            ),
            #
            inference_mode="t1t2",
        )


class Change(Module):
    """The base class of the network architecture of ChangeStar.

    ChangeStar is composed of an any segmentation model and a ChangeMixin module.
    This model is mainly used for binary/multi-class change detection under bitemporal
    supervision and single-temporal supervision. It features the property of
    segmentation architecture reusing, which is helpful to integrate advanced dense
    prediction (e.g., semantic segmentation) network architecture into change detection.

    For multi-class change detection, semantic change prediction can be inferred by a
    binary change prediction from the ChangeMixin module and two semantic predictions
    from the Segmentation model.

    If you use this model in your research, please cite the following paper:

    * https://arxiv.org/abs/2108.07002
    """

    def __init__(
        self,
        dense_feature_extractor: Module,
        changemixin: ChangeMixin,
        inference_mode: str = "t1t2",
    ) -> None:
        """Initializes a new ChangeStar model.

        Args:
            dense_feature_extractor: module for dense feature extraction, typically a
                semantic segmentation model without semantic segmentation head.
            seg_classifier: semantic segmentation head, typically a convolutional layer
                followed by an upsampling layer.
            changemixin: :class:`torchgeo.models.ChangeMixin` module
            inference_mode: name of inference mode ``'t1t2'`` | ``'t2t1'`` | ``'mean'``.
                ``'t1t2'``: concatenate bitemporal features in the order of t1->t2;
                ``'t2t1'``: concatenate bitemporal features in the order of t2->t1;
                ``'mean'``: the weighted mean of the output of ``'t1t2'`` and ``'t1t2'``
        """
        super().__init__()  # type: ignore[no-untyped-call]
        self.dense_feature_extractor = dense_feature_extractor
        self.changemixin = changemixin
        # self.Att = PAM(in_channels=450, out_channels=450, sizes=[
        #     1, 2, 4, 8], ds=1)
        if inference_mode not in ["t1t2", "t2t1", "mean"]:
            raise ValueError(f"Unknown inference_mode: {inference_mode}")
        self.inference_mode = inference_mode

    def forward(self, x: Tensor) -> Dict[str, Tensor]:
        """Forward pass of the model.
        Args:
            x: a bitemporal input tensor of shape [B, T, C, H, W]

        Returns:
            a dictionary containing
                if training stage, returning
                    bi_seg_logit: bitemporal semantic segmentation logit
                    bi_change_logit: bidirected binary change detection logit
                if inference stage, returning
                    bi_seg_logit: bitemporal semantic segmentation logit
                    change_prob:  binary change detection probability
        """
        # print(x.shape) # torch.Size([2, 2, 3, 512, 512])
        b, t, c, h, w = x.shape
        # 组合双时相输入
        x = rearrange(x, "b t c h w -> (b t) c h w")
        # print(x.shape)  # torch.Size([8, 3, 512, 512])
        # feature extraction 提取双时相特征
        bi_feature = self.dense_feature_extractor(x)
        
        # bi_feature = self.Att(bi_feature)
        # print(bi_feature.shape)
        # sys.exit()
        # print([i.shape for i in bi_feature])  # torch.Size([4, 450, 128, 128])
        # semantic segmentation
        # bi_seg_logit = self.seg_classifier(bi_feature)
        # #
        # bi_seg_logit = rearrange(bi_seg_logit, "(b t) c h w -> b t c h w", t=t)
        bi_feature = rearrange(bi_feature, "(b t) c h w -> b t c h w", t=t)
        # print(bi_feature.shape)  # torch.Size([4, 2, 128, 128, 128])
        # bidirected change detection
        c12, c21 = self.changemixin(bi_feature)
        results: Dict[str, Tensor] = {}
        # 预测输出
        if not self.training:
            #
            if self.inference_mode == "t1t2":
                results.update({"change_prob": c12.sigmoid()})
            elif self.inference_mode == "t2t1":
                results.update({"change_prob": c21.sigmoid()})
            elif self.inference_mode == "mean":
                results.update(
                    {
                        "change_prob": torch.stack([c12, c21], dim=0).sigmoid_().mean(dim=0)
                    }
                )
        else:
            results.update(
                {
                    "bi_change_logit": torch.stack([c12, c21], dim=1),
                }
            )
        return results


class ChangeModel(Change):
    """The network architecture of ChangeStar(FarSeg).

    ChangeStar(FarSeg) is composed of a FarSeg model and a ChangeMixin module.

    If you use this model in your research, please cite the following paper:

    * https://arxiv.org/abs/2108.07002
    """

    def __init__(
        self,
        backbone: str = "hrnet_w30",
        backbone_pretrained: bool = True,
    ) -> None:
        """Initializes a new ChangeStarFarSeg model.

        Args:
            backbone: name of ResNet backbone
            classes: number of output segmentation classes
            backbone_pretrained: whether to use pretrained weight for backbone
        """
        # segmentation model
        if "hrnet" in backbone:
            model = HRNet(backbone, backbone_pretrained)
        # 调用父类的构造函数
        super().__init__(
            dense_feature_extractor=model,  # extract feature
            # change detection module
            changemixin=ChangeMixin(
                in_channels=450*2, inner_channels=16, num_convs=4, scale_factor=4.0
            ),
            inference_mode="mean",
        )

class Change_v1(Module):
    """The base class of the network architecture of ChangeStar.

    ChangeStar is composed of an any segmentation model and a ChangeMixin module.
    This model is mainly used for binary/multi-class change detection under bitemporal
    supervision and single-temporal supervision. It features the property of
    segmentation architecture reusing, which is helpful to integrate advanced dense
    prediction (e.g., semantic segmentation) network architecture into change detection.

    For multi-class change detection, semantic change prediction can be inferred by a
    binary change prediction from the ChangeMixin module and two semantic predictions
    from the Segmentation model.

    If you use this model in your research, please cite the following paper:

    * https://arxiv.org/abs/2108.07002
    """

    def __init__(
        self,
        dense_feature_extractor: Module,
        changemixin: ChangeMixin,
        inference_mode: str = "t1t2",
    ) -> None:
        """Initializes a new ChangeStar model.

        Args:
            dense_feature_extractor: module for dense feature extraction, typically a
                semantic segmentation model without semantic segmentation head.
            seg_classifier: semantic segmentation head, typically a convolutional layer
                followed by an upsampling layer.
            changemixin: :class:`torchgeo.models.ChangeMixin` module
            inference_mode: name of inference mode ``'t1t2'`` | ``'t2t1'`` | ``'mean'``.
                ``'t1t2'``: concatenate bitemporal features in the order of t1->t2;
                ``'t2t1'``: concatenate bitemporal features in the order of t2->t1;
                ``'mean'``: the weighted mean of the output of ``'t1t2'`` and ``'t1t2'``
        """
        super().__init__()  # type: ignore[no-untyped-call]
        self.dense_feature_extractor = dense_feature_extractor
        self.changemixin = changemixin
        # self.Att = PAM(in_channels=450, out_channels=450, sizes=[
        #     1, 2, 4, 8], ds=1)
        if inference_mode not in ["t1t2", "t2t1", "mean"]:
            raise ValueError(f"Unknown inference_mode: {inference_mode}")
        self.inference_mode = inference_mode

    def forward(self, x: Tensor) -> Dict[str, Tensor]:
        """Forward pass of the model.
        Args:
            x: a bitemporal input tensor of shape [B, T, C, H, W]

        Returns:
            a dictionary containing
                if training stage, returning
                    bi_seg_logit: bitemporal semantic segmentation logit
                    bi_change_logit: bidirected binary change detection logit
                if inference stage, returning
                    bi_seg_logit: bitemporal semantic segmentation logit
                    change_prob:  binary change detection probability
        """
        # print(x.shape) # torch.Size([2, 2, 3, 512, 512])
        b, t, c, h, w = x.shape
        # 组合双时相输入
        x = rearrange(x, "b t c h w -> (b t) c h w")
        # print(x.shape)  # torch.Size([8, 3, 512, 512])
        # feature extraction 提取双时相特征
        bi_feature = self.dense_feature_extractor(x)
        
        # bi_feature = self.Att(bi_feature)
        # print(bi_feature.shape)
        # sys.exit()
        # print([i.shape for i in bi_feature])  # torch.Size([4, 450, 128, 128])
        # semantic segmentation
        # bi_seg_logit = self.seg_classifier(bi_feature)
        # #
        # bi_seg_logit = rearrange(bi_seg_logit, "(b t) c h w -> b t c h w", t=t)
        bi_feature = rearrange(bi_feature, "(b t) c h w -> b t c h w", t=t)
        # print(bi_feature.shape)  # torch.Size([4, 2, 128, 128, 128])
        # bidirected change detection
        c12, c21 = self.changemixin(bi_feature)
        results: Dict[str, Tensor] = {}
        # 预测输出
        if not self.training:
            #
            if self.inference_mode == "t1t2":
                results.update({"change_prob": c12.sigmoid()})
            elif self.inference_mode == "t2t1":
                results.update({"change_prob": c21.sigmoid()})
            elif self.inference_mode == "mean":
                results.update(
                    {
                        "change_prob": torch.stack([c12, c21], dim=0).sigmoid_().mean(dim=0)
                    }
                )
        else:
            results.update(
                {
                    "bi_change_logit": torch.stack([c12, c21], dim=1),
                }
            )
        return results


class ChangeModel(Change_v1):
    """The network architecture of ChangeStar(FarSeg).

    ChangeStar(FarSeg) is composed of a FarSeg model and a ChangeMixin module.

    If you use this model in your research, please cite the following paper:

    * https://arxiv.org/abs/2108.07002
    """

    def __init__(
        self,
        backbone: str = "hrnet_w30",
        backbone_pretrained: bool = True,
    ) -> None:
        """Initializes a new ChangeStarFarSeg model.

        Args:
            backbone: name of ResNet backbone
            classes: number of output segmentation classes
            backbone_pretrained: whether to use pretrained weight for backbone
        """
        # segmentation model
        if "hrnet" in backbone:
            model = HRNet(backbone, backbone_pretrained)
        # 调用父类的构造函数
        super().__init__(
            dense_feature_extractor=model,  # extract feature
            # change detection module
            changemixin=ChangeMixin(
                in_channels=450*2, inner_channels=16, num_convs=4, scale_factor=4.0
            ),
            inference_mode="mean",
        )
