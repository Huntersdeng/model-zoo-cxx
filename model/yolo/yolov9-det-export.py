import argparse
import random
from io import BytesIO
from typing import Tuple

import onnx
import torch
from onnx import TensorProto
from ultralytics import YOLO

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Graph, Tensor, Value

try:
    import onnxsim
except ImportError:
    onnxsim = None

from models.experimental import attempt_load

class TRT_NMS(torch.autograd.Function):

    @staticmethod
    def forward(
            ctx: Graph,
            boxes: Tensor,
            scores: Tensor,
            iou_threshold: float = 0.65,
            score_threshold: float = 0.25,
            max_output_boxes: int = 100,
            background_class: int = -1,
            box_coding: int = 0,
            plugin_version: str = '1',
            score_activation: int = 0
    ) -> Tuple[Tensor, Tensor, Tensor, Tensor]:
        batch_size, num_boxes, num_classes = scores.shape
        num_dets = torch.randint(0,
                                 max_output_boxes, (batch_size, 1),
                                 dtype=torch.int32)
        boxes = torch.randn(batch_size, max_output_boxes, 4)
        scores = torch.randn(batch_size, max_output_boxes)
        labels = torch.randint(0,
                               num_classes, (batch_size, max_output_boxes),
                               dtype=torch.int32)

        return num_dets, boxes, scores, labels

    @staticmethod
    def symbolic(
            g,
            boxes: Value,
            scores: Value,
            iou_threshold: float = 0.45,
            score_threshold: float = 0.25,
            max_output_boxes: int = 100,
            background_class: int = -1,
            box_coding: int = 0,
            score_activation: int = 0,
            plugin_version: str = '1') -> Tuple[Value, Value, Value, Value]:
        out = g.op('TRT::EfficientNMS_TRT',
                   boxes,
                   scores,
                   iou_threshold_f=iou_threshold,
                   score_threshold_f=score_threshold,
                   max_output_boxes_i=max_output_boxes,
                   background_class_i=background_class,
                   box_coding_i=box_coding,
                   plugin_version_s=plugin_version,
                   score_activation_i=score_activation,
                   outputs=4)
        nums_dets, boxes, scores, classes = out
        return nums_dets, boxes, scores, classes
    
class ORT_NMS(torch.autograd.Function):
    '''ONNX-Runtime NMS operation'''
    @staticmethod
    def forward(ctx,
                boxes,
                scores,
                max_output_boxes_per_class=torch.tensor([100]),
                iou_threshold=torch.tensor([0.45]),
                score_threshold=torch.tensor([0.25])):
        device = boxes.device
        batch = scores.shape[0]
        num_det = random.randint(0, 100)
        batches = torch.randint(0, batch, (num_det,)).sort()[0].to(device)
        idxs = torch.arange(100, 100 + num_det).to(device)
        zeros = torch.zeros((num_det,), dtype=torch.int64).to(device)
        selected_indices = torch.cat([batches[None], zeros[None], idxs[None]], 0).T.contiguous()
        selected_indices = selected_indices.to(torch.int64)
        return selected_indices

    @staticmethod
    def symbolic(g, boxes, scores, max_output_boxes_per_class, iou_threshold, score_threshold):
        return g.op("NonMaxSuppression", boxes, scores, max_output_boxes_per_class, iou_threshold, score_threshold)

class YOLOv9(nn.Module):
    export = True
    shape = None
    dynamic = True
    iou_thres = 0.65
    conf_thres = 0.25
    topk = 100
    use_trt_nms = False
    use_onnx_nms = False
    def __init__(self, weights, device='cpu'):
        super().__init__()
        self.device = device
        self.model = attempt_load(weights, device=self.device, inplace=True, fuse=True)
        self.convert_matrix = torch.tensor([[1, 0, 1, 0], [0, 1, 0, 1], [-0.5, 0, 0.5, 0], [0, -0.5, 0, 0.5]],
                                           dtype=torch.float32,
                                           device=self.device)
        

    def forward(self, x):
        out, _ = self.model(x)
        bs = out.shape[0]  # batch size
        nc = out.shape[1] - 4  # number of classes
        boxes, scores = out.split((4,nc), 1)
        boxes = (boxes.transpose(1,2) @ self.convert_matrix)

        if self.use_trt_nms:
            return TRT_NMS.apply(boxes, scores.transpose(1, 2),
                                self.iou_thres, self.conf_thres, self.topk)
        elif self.use_onnx_nms:
            max_output_boxes_per_class = torch.tensor([self.topk])
            iou_thres = torch.tensor([self.iou_thres])
            conf_thres = torch.tensor([self.conf_thres])
            num_selected_indices = ORT_NMS.apply(boxes, scores, max_output_boxes_per_class, iou_thres, conf_thres)
            
            scores = scores.transpose(1, 2)
            bbox_result = self.gather(boxes, num_selected_indices)
            score_intermediate_result = self.gather(scores, num_selected_indices).max(axis=-1)
            score_result = score_intermediate_result.values
            classes_result = score_intermediate_result.indices.to(torch.int32)
            num_dets = torch.tensor(score_result.shape[-1]).reshape([1,1]).to(torch.int32).clone().detach()

            return (num_dets, bbox_result, score_result, classes_result)
        else:
            scores, labels = scores.transpose(1, 2).max(dim=-1, keepdim=True)
            return torch.cat([boxes, scores, labels], dim=2)
        
    def gather(self, target, idx):
        pick_indices = idx[:, -1:].repeat(1, target.shape[2]).unsqueeze(0)
        return torch.gather(target, 1, pick_indices)
    
def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-w',
                        '--weights',
                        type=str,
                        required=True,
                        help='PyTorch yolov8 weights')
    parser.add_argument('--trt-nms',
                        action='store_true',
                        required=False,
                        help='Use TensorRT Efficient NMS plugins')
    parser.add_argument('--onnx-nms',
                        action='store_true',
                        required=False,
                        help='Use onnx NMS ops')
    parser.add_argument('--iou-thres',
                        type=float,
                        default=0.65,
                        help='IOU threshoud for NMS plugin')
    parser.add_argument('--conf-thres',
                        type=float,
                        default=0.25,
                        help='CONF threshoud for NMS plugin')
    parser.add_argument('--topk',
                        type=int,
                        default=100,
                        help='Max number of detection bboxes')
    parser.add_argument('--opset',
                        type=int,
                        default=11,
                        help='ONNX opset version')
    parser.add_argument('--sim',
                        action='store_true',
                        help='simplify onnx model')
    parser.add_argument('--input-shape',
                        nargs='+',
                        type=int,
                        default=[1, 3, 640, 640],
                        help='Model input shape only for api builder')
    parser.add_argument('--device',
                        type=str,
                        default='cpu',
                        help='Export ONNX device')
    args = parser.parse_args()
    assert len(args.input_shape) == 4
    YOLOv9.conf_thres = args.conf_thres
    YOLOv9.iou_thres = args.iou_thres
    YOLOv9.topk = args.topk
    YOLOv9.use_trt_nms = args.trt_nms
    YOLOv9.use_onnx_nms = args.onnx_nms
    return args


def export_end2end(args):
    b = args.input_shape[0]
    model = YOLOv9(args.weights)
    model.to(args.device)
    fake_input = torch.randn(args.input_shape).to(args.device)
    for _ in range(2):
        model(fake_input)
    save_path = args.weights[:-3]+ '_end2end.onnx'
    with BytesIO() as f:
        torch.onnx.export(
            model,
            fake_input,
            f,
            opset_version=args.opset,
            input_names=['images'],
            output_names=['num_dets', 'bboxes', 'scores', 'labels'])
        f.seek(0)
        onnx_model = onnx.load(f)
    onnx.checker.check_model(onnx_model)
    shapes = [b, 1, b, args.topk, 4, b, args.topk, b, args.topk]
    for i in onnx_model.graph.output:
        for j in i.type.tensor_type.shape.dim:
            j.dim_param = str(shapes.pop(0))
    if args.sim:
        try:
            onnx_model, check = onnxsim.simplify(onnx_model)
            assert check, 'assert check failed'
        except Exception as e:
            print(f'Simplifier failure: {e}')
    onnx.save(onnx_model, save_path)
    print(f'ONNX export success, saved as {save_path}')

def export_normal(args):
    b = args.input_shape[0]
    model = YOLOv9(args.weights)
    model.to(args.device)
    fake_input = torch.randn(args.input_shape).to(args.device)
    for _ in range(2):
        model(fake_input)
    # save_path = args.weights.replace('.pt', '.onnx')
    save_path = args.weights[:-3] + '_normal.onnx'
    with BytesIO() as f:
        torch.onnx.export(
            model,
            fake_input,
            f,
            opset_version=args.opset,
            input_names=['images'],
            output_names=['output'])
        f.seek(0)
        onnx_model = onnx.load(f)
    onnx.checker.check_model(onnx_model)

    if args.sim:
        try:
            onnx_model, check = onnxsim.simplify(onnx_model)
            assert check, 'assert check failed'
        except Exception as e:
            print(f'Simplifier failure: {e}')
    onnx.save(onnx_model, save_path)
    print(f'ONNX export success, saved as {save_path}')

def main(args):
    if args.trt_nms or args.onnx_nms:
        export_end2end(args)
    else:
        export_normal(args)

if __name__=='__main__':
    main(parse_args())