import os

import torch
from diffusers import LMSDiscreteScheduler, StableDiffusionPipeline
from rtpt import RTPT
from torch import autocast

HF_TOKEN = 'INSERT_HF_TOKEN'
HOMOGLYPHS = (('latin', 'o'), ('african', 'ọ'), ('hangul', 'ㅇ'),
              ('arabic', 'ه'), ('oriya', '୦'), ('osmanya', '𐒆'), ('nko', 'ߋ'),
              ('armenian', 'օ'), ('bengali', '০'))
OUTPUT_FOLDER = 'stable_diffusion_images'
NUM_SAMPLES = 4
SEED = 1


def main():
    lms = LMSDiscreteScheduler(beta_start=0.00085,
                               beta_end=0.012,
                               beta_schedule="scaled_linear")

    rtpt = RTPT('XX', 'Images', len(HOMOGLYPHS))
    rtpt.start()

    pipe = StableDiffusionPipeline.from_pretrained(
        "runwayml/stable-diffusion-v1-5",
        scheduler=lms,
        use_auth_token=HF_TOKEN).to("cuda")

    os.makedirs(OUTPUT_FOLDER, exist_ok=True)

    for script, c in HOMOGLYPHS:
        prompt = f'A photo {c}f an actress'
        file_name = f'actress_{script}'
        torch.manual_seed(SEED)

        for i in range(NUM_SAMPLES):
            with autocast("cuda"):
                image = pipe(prompt, num_inference_steps=100)["sample"][0]

            image.save(f"{OUTPUT_FOLDER}/{file_name}_{i}.jpg")
        rtpt.step()


if __name__ == "__main__":
    main()
