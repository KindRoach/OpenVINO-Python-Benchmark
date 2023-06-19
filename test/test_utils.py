import unittest
from typing import Tuple

import cv2
import numpy
import torch
from PIL import Image
from numpy.testing import assert_array_equal
from openvino.runtime import Core
from torchvision.transforms import transforms

from utils import MODEL_MAP, TEST_IMAGE_PATH, ModelMeta, load_model, preprocess


def torch_predict(model: ModelMeta) -> Tuple[numpy.ndarray, numpy.ndarray]:
    # torchvision preprocess
    input_image = Image.open(TEST_IMAGE_PATH)
    preprocess = transforms.Compose([
        transforms.Resize(model.input_size[-1]),
        transforms.ToTensor(),
        transforms.Normalize(
            mean=model.input_mean,
            std=model.input_std
        ),
    ])

    # load torch model
    weight = model.weight
    load_func = model.load_func
    model = load_func(weights=weight)
    model.eval()

    # torch predict
    input_tensor = preprocess(input_image)
    input_batch = input_tensor.unsqueeze(0)
    with torch.no_grad():
        torch_output = model(input_batch)

    torch_output = torch_output.numpy()
    return torch_output.max(axis=1), torch_output.argmax(axis=1)


def ov_predict(model: ModelMeta, model_type: str, ov_preprocess: bool) -> Tuple[numpy.ndarray, numpy.ndarray]:
    frame = cv2.imread(TEST_IMAGE_PATH)
    if ov_preprocess:
        frame = numpy.expand_dims(frame, 0)
    else:
        frame = preprocess(frame, model)
    model = load_model(Core(), model, model_type, ov_preprocess)
    infer_req = model.create_infer_request()
    infer_req.infer(frame)
    ov_output = infer_req.get_output_tensor().data
    return ov_output.max(axis=1), ov_output.argmax(axis=1)


class TestingUtils(unittest.TestCase):
    """
    By inference on a test image, we compare the output of torch model and OpenVINO fp32/int8 model.
    """

    def test_numpy_preprocess(self):
        for model in MODEL_MAP.values():
            torch_confidence, torch_label = torch_predict(model)
            ov_confidence, ov_label = ov_predict(model, "fp32", False)
            assert_array_equal(torch_label, ov_label)

    def test_ov_preprocess(self):
        for model in MODEL_MAP.values():
            torch_confidence, torch_label = torch_predict(model)
            ov_confidence, ov_label = ov_predict(model, "fp32", True)
            assert_array_equal(torch_label, ov_label)

    def test_ov_quantization(self):
        for model in MODEL_MAP.values():
            torch_confidence, torch_label = torch_predict(model)
            ov_confidence, ov_label = ov_predict(model, "int8", False)
            assert_array_equal(torch_label, ov_label)
