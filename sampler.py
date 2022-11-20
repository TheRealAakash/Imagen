from typing import Tuple
import jax
import flax
from flax import linen as nn
import jax.numpy as jnp

from tqdm import tqdm

from functools import partial
import cv2
import numpy as np


def right_pad_dims_to(x, t):
    padding_dims = t.ndim - x.ndim
    if padding_dims <= 0:
        return t
    return jnp.pad(t, [(0, 0)] * padding_dims + [(0, 0), (0, 0)])


def cosine_beta_schedule(timesteps: int, s: float = 0.008) -> float:
    """Cosine decay schedule."""
    steps = timesteps + 1
    x = jnp.linspace(0, timesteps, steps)
    alphas_cumprod = jnp.cos(((x/timesteps) + s)/(1+s) * jnp.pi/2)
    alphas_cumprod = alphas_cumprod / alphas_cumprod[0]

    betas = 1 - (alphas_cumprod[1:] / alphas_cumprod[:-1])
    return jnp.clip(betas, a_min=0.0001, a_max=0.9999)


def linear_beta_schedule(timesteps: int):
    beta_start = 0.0001
    beta_end = 0.02

    return jnp.linspace(beta_start, beta_end, timesteps)


def extract(a, t, x_shape):
    batch_size = t.shape[0]
    out = jnp.take_along_axis(a, t, -1)
    return jnp.reshape(out, (batch_size, *((1,) * (len(x_shape) - 1))))


class GaussianDiffusionContinuousTimes(nn.Module):
    noise_schedule: str = "cosine"
    num_timesteps: int = 1000

    def setup(self):
        if self.noise_schedule == "linear":
            self.beta_schedule = linear_beta_schedule
        elif self.noise_schedule == "cosine":
            self.beta_schedule = cosine_beta_schedule
        else:
            raise ValueError(f"Unknown noise schedule {self.noise_schedule}")

        self.betas = self.beta_schedule(self.num_timesteps)
        self.alphas = 1 - self.betas
        self.alphas_cumprod = jnp.cumprod(self.alphas)
        self.alphas_cumprod_prev = jnp.pad(
            self.alphas_cumprod, ((1, 0),), constant_values=1.0)
        self.sqrt_recip_alphas = jnp.sqrt(1.0 / self.alphas)

        self.sqrt_alphas_cumprod = jnp.sqrt(self.alphas_cumprod)
        self.sqrt_one_minus_alphas_cumprod = jnp.sqrt(
            1.0 - self.alphas_cumprod)

        self.posteiror_variance = self.betas * \
            (1. - self.alphas_cumprod_prev) / (1. - self.alphas_cumprod)

    def get_times(self):
        return self.beta_schedule(self.num_timesteps)

    def q_sample(self, x_start, t, noise):
        sqrt_alphas_cumprod_t = extract(
            self.sqrt_alphas_cumprod, t, x_start.shape)
        sqrt_one_minus_alphas_cumprod_t = extract(
            self.sqrt_one_minus_alphas_cumprod, t, x_start.shape)
        return sqrt_alphas_cumprod_t * x_start + sqrt_one_minus_alphas_cumprod_t * noise


def get_noisy_image(x, t, noise, sampler):
    return sampler.q_sample(x, t, noise)


def test():
    img = cv2.imread("images.jpeg")
    img = jnp.array([img])
    img /= 255.0
    noise = jax.random.normal(jax.random.PRNGKey(0), img.shape)
    scheduler = GaussianDiffusionContinuousTimes(
        noise_schedule="cosine", num_timesteps=1000)
    images = []
    for i in range(1000):
        x_noise = get_noisy_image(img, jnp.array([i]), noise, scheduler)

        x_noise = x_noise * 255.0
        x_noise = x_noise.astype(jnp.uint8)
        x_noise = np.asarray(x_noise[0])
        images.append(x_noise)
    videoWriter = cv2.VideoWriter("video.mp4", cv2.VideoWriter_fourcc(
        *'MJPG'), 240, (x_noise.shape[1], x_noise.shape[0]))
    for i in range(1000):
        videoWriter.write(images[i])

    videoWriter.release()


if __name__ == "__main__":
    test()
