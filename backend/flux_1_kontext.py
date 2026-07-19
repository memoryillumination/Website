import io
import os
import time
import torch
import threading
from PIL import Image
from diffusers import FluxKontextPipeline, FluxTransformer2DModel, GGUFQuantizationConfig
from transformers import T5EncoderModel
from flask import Flask, request, send_file, jsonify

class LocalColoringModel:
    def __init__(self, model_dir="/home/connor/models"):
        self.model_dir = os.path.abspath(model_dir)
        self.pipe = None
        self.lock = threading.Lock()

    def load_model(self):
        gguf_path = os.path.join(self.model_dir, "flux1-kontext-dev-Q8_0.gguf")
        print(f"Loading Quantized Flux Context model from: {gguf_path}")

        if not os.path.exists(gguf_path):
            raise FileNotFoundError(f"Could not find GGUF file at: {gguf_path}")

        # 1. Load 12 GB GGUF transformer into host RAM
        transformer = FluxTransformer2DModel.from_single_file(
            gguf_path,
            quantization_config=GGUFQuantizationConfig(compute_dtype=torch.bfloat16),
            torch_dtype=torch.bfloat16,
            config="black-forest-labs/FLUX.1-Kontext-dev",
            subfolder="transformer",
            low_cpu_mem_usage=True
        )
        
        print("Isolating Transformer block directly inside VRAM...")
        transformer = transformer.to("xpu")
        torch.xpu.empty_cache()

        # 2. Load the 4.7 GB pre-quantized text encoder into host RAM
        print("Loading pre-quantized FP8 T5 Text Encoder from public layout mirror...")
        text_encoder_2 = T5EncoderModel.from_pretrained(
            "John6666/flux1-dev-fp8-flux",
            subfolder="text_encoder_2",
            torch_dtype=torch.bfloat16,
            low_cpu_mem_usage=True,
            cache_dir=self.model_dir
        )
        
        print("Isolating Text Encoder directly inside VRAM...")
        text_encoder_2 = text_encoder_2.to("xpu")
        torch.xpu.empty_cache()

        # 3. Assemble components
        print("Assembling remaining pipeline components from cache...")
        self.pipe = FluxKontextPipeline.from_pretrained(
            "black-forest-labs/FLUX.1-Kontext-dev",
            transformer=transformer,
            text_encoder_2=text_encoder_2,
            torch_dtype=torch.bfloat16,
            cache_dir=self.model_dir,
            low_cpu_mem_usage=True
        )

        # Disable the internal hardcoded Float32 upcast override
        if hasattr(self.pipe.vae, "config"):
            self.pipe.vae.register_to_config(force_upcast=False)
        self.pipe.vae.to(dtype=torch.bfloat16)
        self.pipe.enable_vae_tiling()

        # This aligns the memory structure perfectly with Intel's XMX hardware execution lanes.
        print("Optimizing tensor execution structures to Channels-Last (NHWC)...")
        self.pipe.transformer.to(memory_format=torch.channels_last)
        self.pipe.vae.to(memory_format=torch.channels_last)

        # Sync complete layout natively inside GPU memory
        print("Syncing complete pipeline layout natively inside GPU memory...")
        self.pipe.to("xpu")

        print("Flux hybrid optimized pipeline successfully locked in VRAM.")

    def process(self, image_bytes: bytes) -> bytes:
        with self.lock:
            t_start = time.perf_counter()

            init_image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            original_size = init_image.size
            input_image = init_image.resize((1024, 1024), Image.LANCZOS)

            t_preprocess = time.perf_counter()

            prompt = "coloring book page, clean line art, black and white"
            negative_prompt = "color, shading, gradients, grayscale, photorealistic, 3d, rendering, shadows, noise, textured paper"

            print("Starting native local inference pass...")
            with torch.inference_mode():
                result = self.pipe(
                    prompt=prompt,
                    negative_prompt=negative_prompt,
                    image=input_image,
                    num_inference_steps=4,
                    guidance_scale=4,
                    generator=torch.Generator(device="xpu").manual_seed(42)
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
