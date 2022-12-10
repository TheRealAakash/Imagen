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
    # extract the values of a at the positions given by t
    # batch_size = t.shape[0] # get the batch size
    batch_size = t.shape[0]  # get the batch size
    out = jnp.take_along_axis(a, t, -1)  # extract the values
    # reshape the output
    return jnp.reshape(out, (batch_size, *((1,) * (len(x_shape) - 1))))


class GaussianDiffusionContinuousTimes(struct.PyTreeNode):
    noise_schedule: str = struct.field(pytree_node=False)
    num_timesteps: int = struct.field(pytree_node=False)
    beta_schedule: Any = struct.field(pytree_node=False)
    
    betas: Any
    alphas: Any
    alphas_cumprod: Any
    alphas_cumprod_prev: Any
    
    sqrt_alphas_cumprod: Any
    sqrt_one_minus_alphas_cumprod: Any
    log_one_minus_alphas_cumprod: Any
    sqrt_recip_alphas_cumprod: Any
    sqrt_recipt_minus_one_alphas_cumprod: Any
    
    # posterior variance
    posterior_variance: Any
    
    posterior_log_variance_clipped: Any
    posterior_mean_coef1: Any
    posterior_mean_coef2: Any
    

    def get_times(self):
        return self.beta_schedule(self.num_timesteps)

    def q_sample(self, x_start, t, noise):
        sqrt_alphas_cumprod_t = extract(
            self.sqrt_alphas_cumprod, t, x_start.shape)
        sqrt_one_minus_alphas_cumprod_t = extract(
            self.sqrt_one_minus_alphas_cumprod, t, x_start.shape)
        return sqrt_alphas_cumprod_t * x_start + sqrt_one_minus_alphas_cumprod_t * noise

    def sample_random_time(self, batch_size, rng):
        return jax.random.randint(rng, (batch_size,), 0, self.num_timesteps)
    
    def predict_start_from_noise(self, x_t, t, noise):
        return(
            extract(self.sqrt_recip_alphas_cumprod,
                    t, x_t.shape) * x_t - extract(self.sqrt_recipt_minus_one_alphas_cumprod, t, x_t.shape) * noise
        )

    def q_posterior(self, x_start, x_t, t):
        posteior_mean = (
            extract(self.posterior_mean_coef1, t, x_start.shape) * x_start + extract(self.posterior_mean_coef2, t, x_start.shape) * x_t
        )
        
        posterior_variance = extract(self.posterior_variance, t, x_start.shape)
        posterior_log_variance_clipped = extract(self.posterior_log_variance_clipped, t, x_start.shape)
        return posteior_mean, posterior_variance, posterior_log_variance_clipped
    
    @classmethod
    def create(cls, noise_schedule, num_timesteps):
        if noise_schedule == "cosine":
            beta_schedule = cosine_beta_schedule
        elif noise_schedule == "linear":
            beta_schedule = linear_beta_schedule
        else:
            ValueError(f"Unknown noise schedule {noise_schedule}")

        betas = beta_schedule(num_timesteps)
        alphas = 1 - betas
        alphas_cumprod = jnp.cumprod(alphas, axis=0)
        alphas_cumprod_prev = jnp.pad(
            alphas_cumprod[:-1], ((1, 0),), constant_values=1.0)

        sqrt_alphas_cumprod = jnp.sqrt(alphas_cumprod)
        sqrt_one_minus_alphas_cumprod = jnp.sqrt(1 - alphas_cumprod)
        log_one_minus_alphas_cumprod = jnp.log(1 - alphas_cumprod)
        sqrt_recip_alphas_cumprod = jnp.sqrt(1/alphas_cumprod)
        sqrt_recipt_minus_one_alphas_cumprod = jnp.sqrt(1/alphas_cumprod - 1)
        # posterior_variance
        posterior_variance = betas * (1. - alphas_cumprod_prev) / (1. - alphas_cumprod)
        
        posterior_log_variance_clipped = jnp.log(jnp.clip(posterior_variance, 1e-20, 1e20))
        posterior_mean_coef1 = betas * jnp.sqrt(alphas_cumprod_prev) / (1. - alphas_cumprod)
        posterior_mean_coef2 = (1 - alphas_cumprod_prev) * jnp.sqrt(alphas) / (1. - alphas_cumprod)
        
        return cls(
            noise_schedule=noise_schedule,
            num_timesteps=num_timesteps,
            beta_schedule=beta_schedule,
            betas=betas,
            alphas=alphas,
            alphas_cumprod=alphas_cumprod,
            alphas_cumprod_prev=alphas_cumprod_prev,
            sqrt_alphas_cumprod=sqrt_alphas_cumprod,
            sqrt_one_minus_alphas_cumprod=sqrt_one_minus_alphas_cumprod,
            log_one_minus_alphas_cumprod=log_one_minus_alphas_cumprod,
            sqrt_recip_alphas_cumprod=sqrt_recip_alphas_cumprod,
            sqrt_recipt_minus_one_alphas_cumprod=sqrt_recipt_minus_one_alphas_cumprod,
            posterior_variance=posterior_variance,
            posterior_log_variance_clipped=posterior_log_variance_clipped,
            posterior_mean_coef1=posterior_mean_coef1,
            posterior_mean_coef2=posterior_mean_coef2
        )


def get_noisy_image(x, t, noise, sampler):
    return sampler.q_sample(x, t, noise)


def test():
    import cv2
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
