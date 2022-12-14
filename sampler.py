from jax import tree_util
from typing import Any, Tuple
import jax
import flax
from flax import linen as nn
import jax.numpy as jnp

from tqdm import tqdm

from functools import partial
import numpy as np
from flax import struct

from einops import rearrange, repeat
from utils import jax_unstack, default


def right_pad_dims_to(x, t):
    padding_dims = x.ndim - t.ndim
    if padding_dims <= 0:
        return t
    return jnp.reshape(t, (*t.shape, *((1,) * padding_dims)))

@jax.jit
def beta_linear_log_snr(t):
    return -jnp.log(jnp.expm1(1e-4 + 10 * (t ** 2)))

@jax.jit
def alpha_cosine_log_snr(t, s: float = 0.008):
    x = ((jnp.cos((t + s) / (1 + s) * jnp.pi * 0.5) ** -2) - 1)
    x = jnp.clip(x, a_min=1e-8, a_max=1e8)
    return -jnp.log(x)


def sigmoid(x):
    return 1 / (1 + jnp.exp(-x))


def log_snr_to_alpha_sigma(log_snr):
    return jnp.sqrt(sigmoid(log_snr)), jnp.sqrt(sigmoid(-log_snr))


class GaussianDiffusionContinuousTimes(struct.PyTreeNode):
    noise_schedule: str = struct.field(pytree_node=False)
    num_timesteps: int = struct.field(pytree_node=False)

    log_snr: Any = struct.field(pytree_node=False)

    def sample_random_timestep(self, batch_size, rng):
        return jax.random.uniform(key=rng, shape=(batch_size,), minval=0, maxval=1)

    def get_sampling_timesteps(self, batch):
        times = jnp.linspace(1., 0., self.num_timesteps + 1)
        times = repeat(times, 't -> b t', b=batch)
        # times = jnp.stack((times[:, :-1], times[:, 1:]), axis=0)
        times = jax_unstack(times, axis=-1)
        times = jnp.array(times)
        return times

    def q_posterior(self, x_start, x_t, t, t_next=None):
        t_next = default(t_next, jnp.maximum(0, (t - 1. / self.num_timesteps)))
        log_snr = self.log_snr(t)
        log_snr_next = self.log_snr(t_next)
        log_snr, log_snr_next = map(partial(right_pad_dims_to, x_t), (log_snr, log_snr_next))

        alpha, sigma = log_snr_to_alpha_sigma(log_snr)
        alpha_next, sigma_next = log_snr_to_alpha_sigma(log_snr_next)

        # c - as defined near eq 33
        c = -jnp.expm1(log_snr - log_snr_next)
        posterior_mean = alpha_next * (x_t * (1 - c) / alpha + c * x_start)

        # following (eq. 33)
        posterior_variance = (sigma_next ** 2) * c
        # add epsilon to q_posterior_variance to avoid numerical issues
        posterior_variance = jnp.maximum(posterior_variance, 1e-8)
        posterior_log_variance_clipped = jnp.log(posterior_variance)
        return posterior_mean, posterior_variance, posterior_log_variance_clipped

    def q_sample(self, x_start, t, noise):
        dtype = x_start.dtype

        if isinstance(t, float):
            batch = x_start.shape[0]
            t = jnp.full((batch,), t, dtype=dtype)
        log_snr = self.log_snr(t)
        log_snr_padded_dim = right_pad_dims_to(x_start, log_snr)
        alpha, sigma = log_snr_to_alpha_sigma(log_snr_padded_dim)

        return alpha * x_start + sigma * noise  # , log_snr, alpha, sigma
    
    def q_sample_from_to(self, x_from, from_t, to_t, noise):
        shape, device, dtype = x_from.shape, x_from.device, x_from.dtype
        batch = shape[0]

        if isinstance(from_t, float):
            from_t = jnp.full((batch,), from_t)

        if isinstance(to_t, float):
            to_t = jnp.full((batch,), to_t)

        log_snr = self.log_snr(from_t)
        log_snr_padded_dim = right_pad_dims_to(x_from, log_snr)
        alpha, sigma =  log_snr_to_alpha_sigma(log_snr_padded_dim)

        log_snr_to = self.log_snr(to_t)
        log_snr_padded_dim_to = right_pad_dims_to(x_from, log_snr_to)
        alpha_to, sigma_to =  log_snr_to_alpha_sigma(log_snr_padded_dim_to)

        return x_from * (alpha_to / alpha) + noise * (sigma_to * alpha - sigma * alpha_to) / alpha

    def predict_start_from_noise(self, x_t, t, noise):
        log_snr = self.log_snr(t)
        log_snr = right_pad_dims_to(x_t, log_snr)
        alpha, sigma = log_snr_to_alpha_sigma(log_snr)
        return (x_t - sigma * noise) / jnp.maximum(alpha, 1e-8)

    @classmethod
    def create(cls, noise_schedule, num_timesteps):
        if noise_schedule == "cosine":
            log_snr = alpha_cosine_log_snr
        elif noise_schedule == "linear":
            ValueError(f"Unknown noise schedule {noise_schedule}")
            log_snr = beta_linear_log_snr
        else:
            ValueError(f"Unknown noise schedule {noise_schedule}")
        return cls(noise_schedule=noise_schedule, num_timesteps=num_timesteps, log_snr=log_snr)


def get_noisy_image(x, t, noise, sampler):
    return sampler.q_sample(x, t, noise)

def test_sample(carry, ts):
    print(ts)
    print(len(ts))
    return carry, ts

def test():
    import cv2
    img = jnp.ones((64, 64, 64, 3))
    img = jnp.array(img)
    img /= 255.0
    noise = jax.random.normal(jax.random.PRNGKey(0), img.shape)
    scheduler = GaussianDiffusionContinuousTimes.create(
        noise_schedule="cosine", num_timesteps=1000)
    ts = scheduler.get_sampling_timesteps(16)
    print(ts)
    print(ts[-1][0] > 0)
    ts = jnp.array(ts)
    jax.lax.scan(test_sample, img, ts)
    quit()
    images = []
    # ts = scheduler.get_sampling_timesteps(64, jax.random.PRNGKey(0))
    ts = 1.
    x_noise, _, _, _ = get_noisy_image(img, ts, noise, scheduler)
    # print(x_noise)
    for i in range(1000):

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
