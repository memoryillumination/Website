import modal
import os
import io

# 1. Define Volume to cache the huge Flux model weights
volume = modal.Volume.from_name("coloring-book-models", create_if_missing=True)
MODEL_DIR = "/models"

# 2. Define Image with necessary dependencies
# Flux requires specific versions of diffusers and quantization libraries.
image = (
    modal.Image.debian_slim()
    .pip_install(
        "torch",
        "transformers",
        "accelerate",
        "sentencepiece",
        "protobuf",
        "huggingface_hub",
        "diffusers"
    )
    .env({"HF_HUB_CACHE": MODEL_DIR})
)

app = modal.App("coloring-book-flux")

@app.cls(
    gpu="A100", 
    image=image,
    volumes={MODEL_DIR: volume},
    secrets=[modal.Secret.from_name("huggingface-secret")],
    scaledown_window=300,
    timeout=600
)
class ColoringModel:
    
    @modal.enter()
    def load_model(self):
        import torch
        from diffusers import FluxKontextPipeline

        model_id = "black-forest-labs/FLUX.1-Kontext-dev"

        print("Loading Flux Context model (Native BFloat16)...")
        
        # 2. Load standard BFloat16
        self.pipe = FluxKontextPipeline.from_pretrained(
            model_id,
            torch_dtype=torch.bfloat16, # Native speed
            cache_dir=MODEL_DIR
        )

        # 3. SPEED OPTIMIZATION: Move everything to GPU immediately
        # This replaces enable_model_cpu_offload()
        self.pipe.to("cuda")
        
        print("Flux model loaded successfully on GPU.")

    @modal.method()
    def process(self, image_bytes):
        import torch
        from PIL import Image

        # ... (Decode/Resize logic stays the same) ...
        init_image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        original_size = init_image.size
        input_image = init_image.resize((1024, 1024), Image.LANCZOS)

        prompt = "coloring book page, clean line art, black and white"
        negative_prompt = "color, shading, gradients, grayscale, photorealistic, 3d, rendering, shadows, noise, textured paper"

        print("Starting inference...")
        with torch.inference_mode():
            result = self.pipe(
                prompt=prompt,
                negative_prompt=negative_prompt,
                image=input_image,
                num_inference_steps=10, 
                guidance_scale=4,
                generator=torch.Generator("cuda").manual_seed(42)
            ).images[0]

        # ... (Return logic stays the same) ...
        final_image = result.resize(original_size, Image.LANCZOS)
        buf = io.BytesIO()
        final_image.save(buf, format="PNG")
        
        return {"flux_sketch": buf.getvalue()}