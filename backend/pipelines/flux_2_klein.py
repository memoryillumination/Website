import io
import os
import torch
import threading
from PIL import Image
from diffusers import Flux2KleinPipeline  # Dropped the single-file/GGUF imports
from transformers import T5EncoderModel
from flask import Flask, request, send_file, jsonify

class LocalColoringModel:
    def __init__(self, model_dir="/mnt/TrueNAS/flux-2-klein"):
        self.model_dir = os.path.abspath(model_dir)
        self.pipe = None
        self.lock = threading.Lock()

    def load_model(self):
        # 1. Load the unquantized 16-bit T5-XXL Text Encoder
        print("Loading complete, unquantized T5-XXL Text Encoder from local directory...")
        text_encoder_2 = T5EncoderModel.from_pretrained(
            "city96/t5-v1_1-xxl-encoder-bf16", 
            torch_dtype=torch.bfloat16,
            low_cpu_mem_usage=True,
            cache_dir=self.model_dir
        )
        
        print("Moving full T5 Text Encoder to VRAM...")
        text_encoder_2 = text_encoder_2.to("xpu")
        torch.xpu.empty_cache()

        # 2. Assemble Pipeline & pull raw, unquantized Transformer shards
        print("Assembling standard Flux2KleinPipeline & pulling raw Transformer shards...")
        # By passing text_encoder_2 here and letting diffusers fetch the native layout, 
        # it bypasses the broken single-file GGUF script parser completely.
        self.pipe = Flux2KleinPipeline.from_pretrained(
            "black-forest-labs/FLUX.2-klein-9B",
            text_encoder_2=text_encoder_2,
            torch_dtype=torch.bfloat16,
            cache_dir=self.model_dir,
            low_cpu_mem_usage=True,
            local_files_only=True,
        )

        # ==============================================================================
        # 🚫 DISABLED OPTIMIZATIONS (Commented out for raw baseline testing)
        # ==============================================================================
        # # Disable the internal hardcoded Float32 upcast override
        # if hasattr(self.pipe.vae, "config"):
        #     self.pipe.vae.register_to_config(force_upcast=False)
        # self.pipe.vae.to(dtype=torch.bfloat16)
        # self.pipe.enable_vae_tiling()
        # self.pipe.transformer.to(memory_format=torch.channels_last)
        # self.pipe.vae.to(memory_format=torch.channels_last)
        # ==============================================================================

        print("Syncing complete pipeline layout natively inside GPU memory...")
        self.pipe.to("xpu")
        print("Raw Flux.2 Klein pipeline loaded.")

    DEFAULT_PROMPT = (
        "coloring book page, clean line art, black and white"
        )

    def process(
        self,
        image_bytes: bytes,
        prompt: str = None,
        num_inference_steps: int = 8,
        guidance_scale: float = 2.0,
    ) -> bytes:
        with self.lock:
            init_image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            original_size = init_image.size
            input_image = init_image.resize((1024, 1024), Image.LANCZOS)

            print(
                f"Starting raw local inference pass "
                f"(steps={num_inference_steps}, guidance_scale={guidance_scale})..."
            )
            with torch.inference_mode():
                result = self.pipe(
                    prompt=prompt or self.DEFAULT_PROMPT,
                    image=input_image,
                    num_inference_steps=num_inference_steps,
                    guidance_scale=guidance_scale,
                    generator=torch.Generator(device="xpu").manual_seed(42)
                ).images[0]

            final_image = result.resize(original_size, Image.LANCZOS)
            buf = io.BytesIO()
            final_image.save(buf, format="PNG")
            return buf.getvalue()

worker_app = Flask(__name__)
model_worker = LocalColoringModel()

@worker_app.route('/generate', methods=['POST'])
def handle_generation_request():
    if 'image' not in request.files:
        return jsonify({"error": "Missing image file parameter"}), 400

    file = request.files['image']
    input_bytes = file.read()

    try:
        output_bytes = model_worker.process(
            input_bytes,
            prompt=request.form.get('prompt'),
            num_inference_steps=int(request.form.get('num_inference_steps', 8)),
            guidance_scale=float(request.form.get('guidance_scale', 2.0)),
        )
        return send_file(io.BytesIO(output_bytes), mimetype='image/png')
    except Exception as e:
        print(f"❌ Worker Generation Fault: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    model_worker.load_model()
    print("🚀 Local Inference Worker live. Listening for internal jobs on port 5001...")
    worker_app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)