import jax
from flax.training import checkpoints
from tqdm import tqdm
import optax
import jax.numpy as jnp
from imagen_main import Imagen
import wandb
import numpy as np
from datasets import load_dataset

from functools import partial
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import io
import urllib

import PIL.Image

from datasets import load_dataset
from datasets.utils.file_utils import get_datasets_user_agent

import dataCollector
import ray
ray.init()

wandb.init(project="imagen", entity="therealaakash")
USER_AGENT = get_datasets_user_agent()



class config:
    batch_size = 64
    seed = 0
    learning_rate = 1e-4
    image_size = 64
    save_every = 1000
    eval_every = 10
    steps = 1_000_000

def train(imagen: Imagen, steps):
    collector = dataCollector.DataManager.remote(num_workers=5, batch_size=config.batch_size)
    collector.start.remote()

    #dl = DataLoader(dataset, batch_size=config.batch_size, shuffle=True)
    # dl = iter(dl)
    pbar = tqdm(range(1, steps * 1000 + 1))
    for step in range(1, steps + 1):
        images, texts = ray.get(collector.get_batch.remote())
        images = jnp.array(images)
        # print(images.shape)
        timesteps = list(range(0, 1000))
        # shuffle timesteps
        timesteps = np.random.permutation(timesteps)
        for ts in timesteps:
            timestep = jnp.ones(config.batch_size) * ts
            # jax.random.randint(imagen.get_key(), (1,), 0, 999)
            timestep = jnp.array(timestep, dtype=jnp.int16)
            metrics = imagen.train_step(
                images, None, timestep)  # TODO: Add text(None)
            wandb.log(metrics)
            pbar.update(1)
        if step % config.eval_every == 0:
            # TODO: Add text(None)
            samples = 4
            imgs = imagen.sample(None, samples)
            # print(imgs.shape) # (4, 64, 64, 3)
            # log as 16 gifs
            gifs = []
            for i in range(samples):
                frames = np.asarray(imgs[i]) # (frames, 64, 64, 3)
                frames = frames * 127.5 + 127.5
                # reshape to (frames, 3, 64, 64)
                frames = np.transpose(frames, (0, 3, 1, 2))
                video = wandb.Video(frames, fps=240, format="240")
                gifs.append(video)
            wandb.log({"samples": gifs})


def main():
    imagen = Imagen()
    train(imagen, config.steps)


main()
