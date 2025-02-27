import os
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from statistics import mean
from threading import Lock
from typing import List

import tqdm
from openvino.runtime import Core, CompiledModel, AsyncInferQueue
from simple_parsing import choice, flag, field, ArgumentParser
from tqdm import tqdm

from utils import read_input_with_time, cal_fps_from_tqdm, MODEL_LIST, load_ov_compiled_model


@dataclass
class Args:
    model: str = choice(*MODEL_LIST, alias=["-m"], default="resnet50")
    model_type: str = choice("fp32", "fp16", "int8", alias=["-mt"], default="int8")
    device: str = field(alias=["-d"], default="CPU")  # The device used for OpenVINO: CPU, GPU, MULTI:CPU,GPU ...
    inference_only: bool = flag(alias=["-io"], default=False)
    run_mode: str = choice("sync", "async", "multi", "one_decode_multi", alias=["-rm"], default="sync")
    n_stream: int = field(alias=["-n"], default=os.cpu_count())
    duration: int = field(alias=["-t"], default=60)


def sync_infer(args: Args, model: CompiledModel, model_cfg: dict) -> list:
    outputs = []
    with tqdm(unit="frame") as pbar:
        infer_req = model.create_infer_request()
        for frame in read_input_with_time(
                args.duration,
                model_cfg["input_size"],
                model_cfg["mean"],
                model_cfg["std"],
                args.inference_only
        ):
            infer_req.infer(frame)
            output = infer_req.get_output_tensor().data
            outputs.append(output)
            pbar.update(1)

    cal_fps_from_tqdm(pbar)
    return outputs


def async_infer(args: Args, model: CompiledModel, model_cfg: dict) -> list:
    outputs = dict()
    lock = Lock()
    with tqdm(unit="frame") as pbar:
        def call_back(request, userdata):
            with lock:
                frame_id = userdata
                outputs[frame_id] = request.get_output_tensor().data
            pbar.update(1)

        infer_queue = AsyncInferQueue(model)
        infer_queue.set_callback(call_back)

        frames = read_input_with_time(
            args.duration,
            model_cfg["input_size"],
            model_cfg["mean"],
            model_cfg["std"],
            args.inference_only
        )
        for i, frame in enumerate(frames):
            infer_queue.start_async(frame, i)

        infer_queue.wait_all()

    return [item for key, item in sorted(outputs.items())]


def one_decode_multi_infer(args: Args, model: CompiledModel, model_cfg: dict):
    """
    Decode video by one thread as producer and infer frame by a pool of threads as consumers.
    The main difference between this function with "async_infer" is that
    inference results could be retrieved in the order of submission as soon as possible.
    """
    thread_local = threading.local()

    def infer_one_frame(frame):
        if not hasattr(thread_local, 'infer_req'):
            thread_local.infer_req = model.create_infer_request()

        infer_req = thread_local.infer_req
        infer_req.start_async(frame)
        infer_req.wait()
        output = infer_req.get_output_tensor().data
        return output

    with ThreadPoolExecutor(args.n_stream) as pool:
        all_start_time = []
        task_queue = queue.Queue(args.n_stream)

        def decode_and_submit():
            frames = read_input_with_time(
                args.duration,
                model_cfg["input_size"],
                model_cfg["mean"],
                model_cfg["std"],
                args.inference_only
            )
            for frame in frames:
                all_start_time.append(time.perf_counter())
                task = pool.submit(infer_one_frame, frame)
                task_queue.put(task)
            task_queue.put(None)

        t = threading.Thread(target=decode_and_submit)
        t.start()

        outputs = []
        all_end_time = []
        with tqdm(unit="frame") as pbar:
            while (task := task_queue.get()) is not None:
                outputs.append(task.result())
                all_end_time.append(time.perf_counter())
                pbar.update(1)

        all_latency = [1000 * (e - s) for e, s in zip(all_end_time, all_start_time)]
        print(f"latency: avg={mean(all_latency):.2f}ms, min={min(all_latency):.2f}ms, max={max(all_latency):.2f}ms")
        cal_fps_from_tqdm(pbar)
        t.join()

        return outputs


def multi_infer(args: Args, model: CompiledModel, model_cfg: dict) -> list:
    with tqdm(unit="frame") as pbar:
        def infer_stream(thread_id: int):
            outputs = []
            infer_req = model.create_infer_request()
            frames = read_input_with_time(
                args.duration,
                model_cfg["input_size"],
                model_cfg["mean"],
                model_cfg["std"],
                args.inference_only
            )

            for frame_id, frame in enumerate(frames):
                infer_req.start_async(frame)
                infer_req.wait()
                output = infer_req.get_output_tensor().data
                outputs.append(output)
                pbar.update(1)
            return outputs

        with ThreadPoolExecutor(args.n_stream) as pool:
            ids = range(args.n_stream)
            outputs = list(pool.map(infer_stream, ids))

    cal_fps_from_tqdm(pbar)
    return outputs


def parse_args(args: List[str]):
    parser = ArgumentParser()
    parser.add_arguments(Args, dest="arguments")
    return parser.parse_args(args).arguments


def main(args: Args) -> None:
    ie = Core()
    throughput_mode = "THROUGHPUT" if args.run_mode in ["async", "multi", "one_decode_multi"] else "LATENCY"
    ie.set_property("CPU", {"PERFORMANCE_HINT": throughput_mode})
    ie.set_property("GPU", {"PERFORMANCE_HINT": throughput_mode})

    compiled_model, model_cfg = load_ov_compiled_model(ie, args.model, args.model_type, args.device)
    globals()[f"{args.run_mode}_infer"](args, compiled_model, model_cfg)


if __name__ == '__main__':
    main(parse_args(sys.argv[1:]))
