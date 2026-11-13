import io
import os
import time
import torch
import threading
from PIL import Image
from diffusers import Flux2KleinPipeline
from flask import Flask, request, send_file, jsonify

MODEL_PATH = "/mnt/TrueNAS/Shares/Models/FLUX.2-klein-4B"


class LocalColoringModel:
    def __init__(self, model_path=MODEL_PATH):
        self.model_path = os.path.abspath(model_path)
        self.pipe = None
        self.lock = threading.Lock()

    def load_model(self):
        if not os.path.isdir(self.model_path):
            raise FileNotFoundError(f"Model folder not found: {self.model_path}")

        print(f"Loading FLUX.2 klein 4B from: {self.model_path}")
        self.pipe = Flux2KleinPipeline.from_pretrained(
            self.model_path,
            torch_dtype=torch.bfloat16,
            local_files_only=True,
        )

        print("Moving pipeline to XPU...")
        self.pipe.to("xpu")
        torch.xpu.empty_cache()

        print("FLUX.2 klein pipeline successfully loaded in VRAM.")

    def process(self, image_bytes: bytes) -> bytes:
        with self.lock:
            t_start = time.perf_counter()

            init_image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            original_size = init_image.size
            input_image = init_image.resize((1024, 1024), Image.LANCZOS)

            t_preprocess = time.perf_counter()

            prompt = (
                "Convert this photo into a black-and-white coloring book page with "
                "clean, bold outlines, no shading, no gray fill, white background"
            )

            print("Starting native local inference pass...")
            with torch.inference_mode():
                result = self.pipe(
                    prompt=prompt,
                    image=input_image,
                    num_inference_steps=5,
                    guidance_scale=1.0,
                    generator=torch.Generator(device="xpu"),
                ).images[0]

            t_inference = time.perf_counter()

            final_image = result.resize(original_size, Image.LANCZOS)
            buf = io.BytesIO()
            final_image.save(buf, format="PNG")

            t_postprocess = time.perf_counter()

            print(
                "⏱️  local worker timing — "
                f"preprocess: {t_preprocess - t_start:.3f}s, "
                f"inference: {t_inference - t_preprocess:.3f}s, "
                f"postprocess: {t_postprocess - t_inference:.3f}s, "
                f"total: {t_postprocess - t_start:.3f}s"
            )

            return buf.getvalue()


# ==============================================================================
# 🚀 MICROSERVICE WORKER RUNTIME LOOPS
# ==============================================================================
worker_app = Flask(__name__)
model_worker = LocalColoringModel()


@worker_app.route('/generate', methods=['POST'])
def handle_generation_request():
    if 'image' not in request.files:
        return jsonify({"error": "Missing image file parameter"}), 400

    file = request.files['image']
    input_bytes = file.read()

    try:
        output_bytes = model_worker.process(input_bytes)
        return send_file(io.BytesIO(output_bytes), mimetype='image/png')
    except Exception as e:
        print(f"❌ Worker Generation Fault: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    model_worker.load_model()
    print("🚀 Local Inference Worker live. Listening for internal jobs on port 5001...")
    worker_app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)