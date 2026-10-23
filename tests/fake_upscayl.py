"""Stand-in for upscayl-bin.exe used by the tests.

Mimics the real CLI: -h help text, folder input, NN,NN% progress and '... -> <out> done' lines on
stderr. Nearest-neighbour upscale. Behaviour knobs (environment variables):
  FAKE_DELAY  seconds to sleep per image (default 0)
  FAKE_EXIT   process exit code (default 0)
  FAKE_QUIET  1 = no '-> file done' lines (like the real upscayl-bin without -v)
Files whose name contains FAIL are skipped (simulated failure).
"""
import os
import sys
import time

from PIL import Image

HELP = """Usage: upscayl-bin -i infile -o outfile [options]...
  -h                   show this help
  -i input-path        input image path (jpg/png/webp) or directory
  -o output-path       output image path (jpg/png/webp) or directory
  -s output-scale      custom output scale (can be 2, 3, 4. default=4)
  -m model-path        folder path to the pre-trained models. default=models
  -n model-name        model name
  -g gpu-id            gpu device to use (default=auto)
  -j load:proc:save    thread count for load/proc/save (default=1:2:2)
  -f format            output image format (jpg/png/webp, default=ext/png)
"""


def err(text):
    sys.stderr.buffer.write((text + "\n").encode("utf-8"))
    sys.stderr.buffer.flush()


def main(argv):
    if "-h" in argv:
        err(HELP)
        return 0
    opts = dict(zip(argv[::2], argv[1::2]))
    src, dst, scale = opts["-i"], opts["-o"], int(opts.get("-s", 4))
    delay = float(os.environ.get("FAKE_DELAY", "0"))
    files = sorted(f for f in os.listdir(src) if f.lower().endswith(".png"))
    for n, name in enumerate(files):
        time.sleep(delay)
        err(f"{100 * n / len(files):.2f}%".replace(".", ","))
        if "FAIL" in name:
            err(f"decode image {name} failed")
            continue
        with Image.open(os.path.join(src, name)) as im:
            im.resize((im.width * scale, im.height * scale), Image.NEAREST).save(os.path.join(dst, name))
        if os.environ.get("FAKE_QUIET") != "1":
            err(f"✅ {src}/{name} -> {dst}/{name} done".replace("\\", "/"))
    err("\U0001f64c Upscayled Successfully!")
    return int(os.environ.get("FAKE_EXIT", "0"))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
