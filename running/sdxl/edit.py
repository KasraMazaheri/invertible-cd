import argparse
import functools
import torch
import os
import pandas as pd
import torch.distributed as dist
import numpy as np

from tqdm import tqdm
from PIL import Image
from utils.loading import load_models_xl
from utils import generation_sdxl

# Utils
# -------------------------------------------------------------------------------------
def str2bool(v):
    if isinstance(v, bool):
        return v
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')

def prepare_val_prompts(all_text, bs=20, max_cnt=5000):
    all_text = all_text[:max_cnt]

    num_batches = ((len(all_text) - 1) // (bs * dist.get_world_size()) + 1) * dist.get_world_size()
    all_batches = np.array_split(np.array(all_text), num_batches)
    rank_batches = all_batches[dist.get_rank():: dist.get_world_size()]

    index_list = np.arange(len(all_text))
    all_batches_index = np.array_split(index_list, num_batches)
    rank_batches_index = all_batches_index[dist.get_rank():: dist.get_world_size()]
    return rank_batches, rank_batches_index, all_text
# -------------------------------------------------------------------------------------


# Arguments parser
# -------------------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser()

    # Loading settings
    ################################
    parser.add_argument(
        "--model_id",
        type=str,
        default=None,
        required=True,
        help="Path to pretrained DM",
    )
    parser.add_argument(
        "--reverse_checkpoint",
        type=str,
        default=None,
        help="Path to reverse CD",
    )
    parser.add_argument(
        "--forward_checkpoint",
        type=str,
        default=None,
        help="Path to forward CD",
    )
    parser.add_argument(
        "--teacher_checkpoint",
        type=str,
        default=None,
        help="Path to teacher DM with w embedding",
    )
    parser.add_argument(
        "--path_to_prompts",
        type=str,
        default=None,
        required=True,
        help="Path to prompts for benchmarking",
    )
    parser.add_argument(
        "--path_to_images",
        type=str,
        default=None,
        required=True,
        help="Path to images for benchmarking",
    )
    ################################

    # Generation settings
    ################################
    parser.add_argument(
        "--n_steps",
        type=int,
        default=4,
        required=True,
        help="number of steps",
    )
    parser.add_argument(
        "--reverse_timesteps",
        default='',
        nargs='+',
        required=False,
    )
    parser.add_argument(
        "--forward_timesteps",
        '--list',
        default='',
        nargs='+',
        required=False,
    )
    parser.add_argument(
        "--use_dynamic_guidance",
        type=str2bool,
        default='True',
        required=True,
        help='whether to use dynamic guidance for editing'
    )
    parser.add_argument(
        "--tau1",
        type=float,
        default=0.8,
        required=True,
        help="first hyperparameter for dynamic guidance",
    )
    parser.add_argument(
        "--tau2",
        type=float,
        default=0.8,
        required=True,
        help="second hyperparameter for dynamic guidance",
    )
    parser.add_argument(
        "--guidance_scale",
        type=float,
        default=7.0,
        required=True,
        help="guidance scale for editing",
    )
    parser.add_argument(
        "--batch_per_gpu",
        type=int,
        default=32,
    )
    parser.add_argument(
        "--max_cnt",
        type=int,
        default=5000,
    )
    parser.add_argument(
        "--saving_dir",
        type=str,
        required=True,
        default='results',
    )
    ################################

    args = parser.parse_args()

    return args
# -------------------------------------------------------------------------------------

# Running
# -------------------------------------------------------------------------------------
@torch.no_grad()
def main(args):
    model_id = args.model_id
    teacher_checkpoint = args.teacher_checkpoint
    reverse_checkpoint = args.reverse_checkpoint
    forward_checkpoint = args.forward_checkpoint

    stable_pipe, pipe, forward_pipe = load_models_xl(model_id=model_id,
                                                     reverse_checkpoint=reverse_checkpoint,
                                                     forward_checkpoint=forward_checkpoint,
                                                     teacher_checkpoint=teacher_checkpoint)
    text_encoders = [pipe.text_encoder, pipe.text_encoder_2]
    tokenizers = [pipe.tokenizer, pipe.tokenizer_2]

    compute_embeddings_fn = functools.partial(
        generation_sdxl.compute_embeddings,
        proportion_empty_prompts=0,
        text_encoders=text_encoders,
        tokenizers=tokenizers,
    )

    root_1 = args.path_to_prompts
    root_2 = args.path_to_images

    df = pd.read_csv(f'{root_1}')
    imgs = np.array(list(df['file_name']))
    prompts = np.array(list(df['old_caption']))
    edited_prompts = np.array(list(df['edited_caption']))

    outdir_images = f'{args.saving_dir}/generated_images'
    os.makedirs(outdir_images, exist_ok=True)
    if len(args.reverse_timesteps) > 0 and len(args.forward_timesteps) > 0:
        reverse_timesteps = [int(i) for i in args.reverse_timesteps]
        forward_timesteps = [int(i) for i in args.forward_timesteps]
    else:
        reverse_timesteps = None
        forward_timesteps = None

    for j, img in tqdm(enumerate(imgs)):
        init_image = Image.open(f'{root_2}/{img}').resize((1024, 1024))
        init_image = forward_pipe.image_processor.preprocess(init_image)
        prompt = prompts[j]
        generator = torch.Generator(device="cuda").manual_seed(j)

        latents = generation_sdxl.inverse_sample_deterministic(
            forward_pipe,
            init_image,
            [prompt],
            num_inference_steps=args.n_steps,
            timesteps=forward_timesteps,
            generator=generator,
            guidance_scale=0.0,
            is_sdxl=True,
            compute_embeddings_fn=compute_embeddings_fn,
            seed=j
        )
        images = generation_sdxl.sample_deterministic(
            pipe,
            [edited_prompts[j]],
            latents=latents,
            timesteps=reverse_timesteps,
            num_inference_steps=args.n_steps,
            generator=generator,
            guidance_scale=args.guidance_scale,
            is_sdxl=True,
            compute_embeddings_fn=compute_embeddings_fn,
            use_dynamic_guidance=args.use_dynamic_guidance,
            tau1=args.tau1,
            tau2=args.tau2,
            amplify_prompt=[prompt]
        )
        images[0].resize((1024, 1024)).save(f'{outdir_images}/{j}.jpg')
# -------------------------------------------------------------------------------------

if __name__ == "__main__":
    args = parse_args()
    main(args)