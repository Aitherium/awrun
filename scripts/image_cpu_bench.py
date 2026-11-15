"""Measure CPU image generation on one hosted runner: seconds per image and peak RSS.

    python scripts/image_cpu_bench.py openvino-sd-turbo OUT_DIR
    python scripts/image_cpu_bench.py sana-sprint OUT_DIR
    python scripts/image_cpu_bench.py sdcpp OUT_DIR --sd-bin ./sd --model sd_turbo.safetensors

Each engine loads its model ONCE, warms up once, then makes 3 images at 320 px and
3 at 512 px. Writes OUT_DIR/<engine>.json and the PNGs. A failure is recorded in the
JSON with the error and exit code 1; it is never reported as a number.
"""

from __future__ import annotations

import argparse
import json
import resource
import subprocess
import sys
import time
from pathlib import Path

PROMPTS = ["a cute cartoon fox sticker, thick white outline, flat colors",
           "a happy green frog holding a coffee cup, sticker, white border",
           "a tiny robot waving, kawaii sticker, bold outline"]
SIZES = (320, 512)


def peak_rss_mb() -> float:
    self_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    child_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return round(max(self_kb, child_kb) / 1024.0, 1)


def run_diffusers(engine: str, out: Path) -> dict:
    import torch

    torch.set_num_threads(max(1, torch.get_num_threads()))
    t0 = time.time()
    if engine == "openvino-sd-turbo":
        from optimum.intel import OVStableDiffusionPipeline

        model_id = "rupeshs/sd-turbo-openvino"
        pipe = OVStableDiffusionPipeline.from_pretrained(model_id, ov_config={"CACHE_DIR": ""})
        steps, guidance = 1, 0.0
        licence = "sd-turbo: Stability AI Community License (see model card)"
    elif engine == "sana-sprint":
        from diffusers import SanaSprintPipeline

        model_id = "Efficient-Large-Model/Sana_Sprint_0.6B_1024px_diffusers"
        pipe = SanaSprintPipeline.from_pretrained(model_id, torch_dtype=torch.bfloat16)
        pipe.to("cpu")
        steps, guidance = 2, 4.5
        licence = "Sana Sprint: NVIDIA license, see model card (text encoder Gemma 2)"
    else:
        raise SystemExit(f"unknown engine {engine}")
    load_s = round(time.time() - t0, 1)

    def gen(prompt: str, size: int):
        kw = dict(prompt=prompt, num_inference_steps=steps, height=size, width=size)
        if engine == "sana-sprint":
            kw["guidance_scale"] = guidance
        else:
            kw["guidance_scale"] = guidance
        return pipe(**kw).images[0]

    gen(PROMPTS[0], 320)  # warm-up, not timed
    rows = []
    for size in SIZES:
        times = []
        for i, prompt in enumerate(PROMPTS):
            t = time.time()
            img = gen(prompt, size)
            times.append(round(time.time() - t, 2))
            img.save(out / f"{engine}-{size}-{i}.png")
        rows.append({"size": size, "seconds": times,
                     "mean_s": round(sum(times) / len(times), 2)})
    return {"model": model_id, "steps": steps, "load_s": load_s, "results": rows,
            "licence": licence}


def run_sdcpp(sd_bin: str, model: str, out: Path) -> dict:
    rows = []
    for size in SIZES:
        times = []
        for i, prompt in enumerate(PROMPTS):
            t = time.time()
            r = subprocess.run([sd_bin, "-m", model, "-p", prompt, "--steps", "1",
                                "--cfg-scale", "1.0", "-W", str(size), "-H", str(size),
                                "-o", str(out / f"sdcpp-{size}-{i}.png")],
                               capture_output=True, text=True, encoding="utf-8")
            if r.returncode != 0:
                raise RuntimeError(f"sd exited {r.returncode}: {r.stderr[-800:]}")
            times.append(round(time.time() - t, 2))
        rows.append({"size": size, "seconds": times,
                     "mean_s": round(sum(times) / len(times), 2),
                     "note": "includes model load per image (sd.cpp CLI is one-shot)"})
    return {"model": model, "steps": 1, "results": rows,
            "licence": "sd-turbo: Stability AI Community License (see model card)"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("engine")
    ap.add_argument("out")
    ap.add_argument("--sd-bin", default="")
    ap.add_argument("--model", default="")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rec: dict = {"engine": a.engine}
    code = 0
    try:
        if a.engine == "sdcpp":
            rec.update(run_sdcpp(a.sd_bin, a.model, out))
        else:
            rec.update(run_diffusers(a.engine, out))
        rec["ok"] = True
    except Exception as e:  # recorded, never a number
        rec.update(ok=False, error=f"{type(e).__name__}: {str(e)[-1500:]}")
        code = 1
    rec["peak_rss_mb"] = peak_rss_mb()
    (out / f"{a.engine}.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
    print(json.dumps(rec, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
