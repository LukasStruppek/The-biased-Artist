import csv
import math
import os

import numpy as np
import open_clip
import torch
import wandb
from rtpt import RTPT
from transformers import CLIPTextModel

from utils.stable_diffusion_utils import generate

HF_TOKEN = 'INSERT_HF_TOKEN'
HOMOGLYPHS = [('Greek', 'ο'), ('Cyrillic', 'о'), ('Arabic', 'ه'),
              ('Korean', 'ㅇ'), ('African', 'ọ')]

TEMPLATES = [('People', 'relative_bias_prompts/template_people.txt'),
             ('Buildings', 'relative_bias_prompts/template_buildings.txt'),
             ('Misc', 'relative_bias_prompts/template_misc.txt')]
ENCODER_RUN_PATH = None
OUTPUT_FILE = 'rb_results.csv'


def compute_rcb(model, preprocess, x_clean, x_homoglyph, z_target, batch_size):
    similarities = []
    for batch in range(math.ceil(len(x_clean) / batch_size)):
        img_clean_batch = x_clean[batch * batch_size:(batch + 1) * batch_size]
        img_homoglyph_batch = x_homoglyph[batch * batch_size:(batch + 1) *
                                          batch_size]
        img_clean_batch = [
            preprocess(img).unsqueeze(0) for img in img_clean_batch
        ]
        img_homoglyph_batch = [
            preprocess(img).unsqueeze(0) for img in img_homoglyph_batch
        ]
        img_clean_batch = torch.cat(img_clean_batch, dim=0)
        img_homoglyph_batch = torch.cat(img_homoglyph_batch, dim=0)
        text_batch = z_target[batch * batch_size:(batch + 1) * batch_size]
        text_batch = open_clip.tokenize(text_batch)

        with torch.no_grad(), torch.cuda.amp.autocast():
            image_features_clean = model.encode_image(img_clean_batch)
            image_features_homoglyph = model.encode_image(img_homoglyph_batch)
            text_features = model.encode_text(text_batch)

            for feat_clean, feat_homoglyph, feat_text in zip(
                    image_features_clean, image_features_homoglyph,
                    text_features):
                feat_clean /= feat_clean.norm(dim=-1, keepdim=True)
                feat_homoglyph /= feat_homoglyph.norm(dim=-1, keepdim=True)
                feat_text /= feat_text.norm(dim=-1, keepdim=True)

                similarity_clean = (100.0 * feat_clean @ feat_text.T)
                similarity_homoglyph = (100.0 * feat_homoglyph @ feat_text.T)
                rcb = 100.0 * (similarity_homoglyph -
                               similarity_clean) / similarity_clean
                similarities.append(rcb.cpu().item())

    similarities = np.mean(similarities)
    return similarities


def generate_clean_samples(prompt_file, text_encoder, num_images, batch_size):
    with open(prompt_file, 'r') as f:
        clean_prompts = f.readlines()
    clean_prompts = [p.replace('\n', '') for p in clean_prompts]
    clean_prompts = [p.replace('# ', '') for p in clean_prompts]
    clean_prompts = [item for item in clean_prompts for i in range(num_images)]

    clean_images = []
    generator = torch.manual_seed(0)
    for batch in range(math.ceil(len(clean_prompts) / batch_size)):
        clean_images += generate(clean_prompts[batch * batch_size:(batch + 1) *
                                               batch_size],
                                 HF_TOKEN,
                                 text_encoder=text_encoder,
                                 samples=1,
                                 num_inference_steps=100,
                                 guidance_scale=7.5,
                                 generator=generator)
        print('Num clean images: ', len(clean_images))
    return clean_images


def generate_homoglyph_samples(prompt_file, text_encoder, num_images,
                               batch_size, homoglyph):
    with open(prompt_file, 'r') as f:
        homoglyph_prompts = f.readlines()
    homoglyph_prompts = [p.replace('#', homoglyph) for p in homoglyph_prompts]
    homoglyph_prompts = [p.replace('\n', '') for p in homoglyph_prompts]
    homoglyph_prompts = [
        item for item in homoglyph_prompts for i in range(num_images)
    ]

    homoglyph_images = []
    generator = torch.manual_seed(0)
    for batch in range(math.ceil(len(homoglyph_prompts) / batch_size)):
        homoglyph_images += generate(
            homoglyph_prompts[batch * batch_size:(batch + 1) * batch_size],
            HF_TOKEN,
            text_encoder=text_encoder,
            samples=1,
            num_inference_steps=100,
            guidance_scale=7.5,
            generator=generator)
    print('Num homoglyph images: ', len(homoglyph_images))
    return homoglyph_images


def get_target_prompts(prompt_file, target_culture, num_images):
    with open(prompt_file, 'r') as f:
        target_prompts = f.readlines()
    target_prompts = [p.replace('\n', '') for p in target_prompts]
    target_prompts = [p.replace('#', target_culture) for p in target_prompts]
    target_prompts = [
        item for item in target_prompts for i in range(num_images)
    ]
    return target_prompts


def load_wandb_model(run_path, replace=True):
    api = wandb.Api(timeout=60)
    run = api.run(run_path)
    model_path = run.summary["model_save_path"]

    wandb.restore(os.path.join(model_path, 'config.json'),
                  run_path=run_path,
                  root='./weights',
                  replace=replace)
    wandb.restore(os.path.join(model_path, 'pytorch_model.bin'),
                  run_path=run_path,
                  root='./weights',
                  replace=replace)

    encoder = CLIPTextModel.from_pretrained(
        os.path.join('./weights', model_path))

    return encoder


def main():
    num_images = 10
    batch_size = 8

    model, _, preprocess = open_clip.create_model_and_transforms(
        'ViT-H-14', pretrained='laion2b_s32b_b79k')

    if ENCODER_RUN_PATH is not None:
        text_encoder = load_wandb_model(ENCODER_RUN_PATH, replace=True)
    else:
        text_encoder = CLIPTextModel.from_pretrained(
            "openai/clip-vit-large-patch14")

    with open(OUTPUT_FILE, 'a') as f:
        header_dict = {'People': 0, 'Buildings': 0, 'Misc': 0}
        w = csv.DictWriter(f, header_dict.keys())
        w.writeheader()

    rtpt = RTPT('XX', 'compute_bias', len(HOMOGLYPHS))
    rtpt.start()

    clean_sample_dict = {}
    for name, template_prompts in TEMPLATES:
        clean_samples = generate_clean_samples(template_prompts, text_encoder,
                                               num_images, batch_size)
        clean_sample_dict[name] = clean_samples

    for culture, homoglyph in HOMOGLYPHS:

        results = {}

        for name, template_prompts in TEMPLATES:

            homoglyph_samples = generate_homoglyph_samples(
                template_prompts, text_encoder, num_images, batch_size,
                homoglyph)
            target_prompts = get_target_prompts(template_prompts, culture,
                                                num_images)

            rb = compute_rcb(model, preprocess, clean_sample_dict[name],
                             homoglyph_samples, target_prompts, batch_size)
            print(culture, name, rb)
            results[name] = rb

        with open(OUTPUT_FILE, 'a') as f:
            w = csv.DictWriter(f, results.keys())
            print(results)
            w.writerow(results)
        rtpt.step()


if __name__ == '__main__':
    main()
