from functools import partial
from modeling_imagen import EfficentUNet
from typing import Any, Dict, Tuple
import jax
import flax
from flax import linen as nn
import jax.numpy as jnp

from tqdm import tqdm

import optax

from sampler import GaussianDiffusionContinuousTimes, extract
from einops import rearrange, repeat, reduce, pack, unpack
from flax.training import train_state

from jax import tree_util
from flax import struct, jax_utils
from jax.experimental import maps

import numpy as np

from jax.experimental import pjit, PartitionSpec as P

import partitioning as nnp

from flax.linen import partitioning as nn_partitioning

mesh_shape = (2, 4)

class ImagenState(struct.PyTreeNode):
    train_state: train_state.TrainState
    sampler: GaussianDiffusionContinuousTimes
    conditional_drop_prob: float

    def get_key(self):
        rng, key = jax.random.split(self.rng)
        return self.replace(rng=rng), key
    
class GeneratorState(struct.PyTreeNode):
    imagen_state: ImagenState
    image: jnp.ndarray
    text: jnp.ndarray
    attention: jnp.ndarray
    rng: jax.random.PRNGKey

def conditioning_pred(generator_state, t, cond_scale):
    pred = generator_state.imagen_state.train_state.apply_fn({"params": generator_state.imagen_state.train_state.params}, generator_state.image, t, generator_state.text, generator_state.attention, 0.0, generator_state.rng)
    null_logits = generator_state.imagen_state.train_state.apply_fn({"params": generator_state.imagen_state.train_state.params}, generator_state.image, t, generator_state.text, generator_state.attention, 1.0, generator_state.rng) 
    return null_logits + (pred - null_logits) * cond_scale

def p_mean_variance(t_index, generator_state):
    t_index = 999-t_index
    t = jnp.ones(1, dtype=jnp.int16) * t_index
    pred = conditioning_pred(generator_state, t, 4.0)
    x_start = generator_state.imagen_state.sampler.predict_start_from_noise(generator_state.image, t=t, noise=pred)
    
    s = jnp.percentile(
        jnp.abs(rearrange(x_start, 'b ... -> b (...)')),
        0.95,
        axis=-1
    ) # dynamic thresholding percentile
    
    s = jnp.maximum(s, 1.0)
    x_start = jnp.clip(x_start, -s, s) / s
    
    return generator_state.imagen_state.sampler.q_posterior(x_start, x_t=generator_state.image, t=t)

def p_sample(t_index, generator_state):
    model_mean, _, model_log_variance = p_mean_variance(t_index, generator_state)
    rng, key = jax.random.split(generator_state.rng)
    generator_state = generator_state.replace(rng=rng)
    noise = jax.random.normal(key, generator_state.image.shape) 
    x = jax.lax.cond(t_index > 0, lambda x: model_mean + noise * jnp.exp(0.5 * model_log_variance), lambda x: model_mean, None)
    return generator_state.replace(image=x)

def p_sample_loop(imagen_state, img, texts, attention, rng):
    # img is x0
    batch_size = img.shape[0]
    rng, key = jax.random.split(rng)
    # imgs = []
    generator_state = GeneratorState(imagen_state=imagen_state, image=img, text=texts, attention=attention, rng=rng)
    generator_state = jax.lax.fori_loop(0, 1000, p_sample, generator_state)
    img = generator_state.image
    #for i in reversed(range(sampler.num_timesteps)):
     #  rng, key = jax.random.split(rng)
     #  img = p_sample(imagen_state, sampler, img, texts, jnp.ones(batch_size, dtype=jnp.int16) * i, i, key)
       # imgs.append(img)
    # frames, batch, height, width, channels
    # reshape batch, frames, height, width, channels
    return img


def sample(imagen_state, noise, texts, attention, rng):
    return p_sample_loop(imagen_state, noise, texts, attention, rng)

def train_step(imagen_state, imgs_start, timestep, texts, attention_masks, rng):
    rng,key = jax.random.split(rng)
    rng, key2 = jax.random.split(rng)
    noise = jax.random.normal(key, imgs_start.shape)
    x_noise = imagen_state.sampler.q_sample(imgs_start, timestep, noise)
    def loss_fn(params):
        predicted_noise = imagen_state.train_state.apply_fn({"params": params}, x_noise, timestep, texts, attention_masks, imagen_state.conditional_drop_prob, key2)
        loss = jnp.mean((noise - predicted_noise) ** 2)
        return loss, predicted_noise
    gradient_fn = jax.value_and_grad(loss_fn, has_aux=True)
    (loss, logits), grads = gradient_fn(imagen_state.train_state.params)
    train_state = imagen_state.train_state.apply_gradients(grads=grads)
    imagen_state = imagen_state.replace(train_state=train_state)
    return imagen_state, compute_metrics(loss, logits)



class Imagen:
    def __init__(self, img_size: int = 64, batch_size: int = 16, sequence_length: int = 256, encoder_latent_dims: int = 512, num_timesteps: int = 1000, loss_type: str = "l2", conditional_drop_prob=0.1):
        self.random_state = jax.random.PRNGKey(0)        
        self.lowres_scheduler = GaussianDiffusionContinuousTimes.create(
            noise_schedule="cosine", num_timesteps=1000
        )
                
        devices = np.asarray(jax.devices()).reshape(*mesh_shape)
        self.mesh = maps.Mesh(devices, ("X", "Y"))
        
        self.unet = EfficentUNet(max_token_len=sequence_length)
        self.random_state, key = jax.random.split(self.random_state)
        
        params = jax.eval_shape(self.unet.init, key, jnp.ones((batch_size, img_size, img_size, 3)), jnp.ones(batch_size, dtype=jnp.int16), jnp.ones((batch_size, sequence_length, encoder_latent_dims)), jnp.ones((batch_size, sequence_length)), 0.1, self.random_state)
        params_axes = params["params_axes"]
        params_axes = nnp.get_params_axes(params, params_axes, rules=nnp.DEFAULT_TPU_RULES)
        with self.mesh, nn_partitioning.axis_rules(nnp.DEFAULT_TPU_RULES):
            params = pjit.pjit(self.unet.init, in_axis_resources=(P("X", None, None, "Y"), P("X"), P("X", None, "Y"), P("X", "Y"), None, None), out_axis_resources=params_axes)(key, jnp.ones((batch_size, img_size, img_size, 3)), jnp.ones(batch_size, dtype=jnp.int16), jnp.ones((batch_size, sequence_length, encoder_latent_dims)), jnp.ones((batch_size, sequence_length)), 0.1, self.random_state)
        print(params)
        # self.params = self.unet.init(key, jnp.ones((batch_size, img_size, img_size, 3)), jnp.ones(batch_size, dtype=jnp.int16), jnp.ones((batch_size, sequence_length, encoder_latent_dims)), jnp.ones((batch_size, sequence_length)), 0.1, key)
        
        quit()
        
        lr = optax.warmup_cosine_decay_schedule(
            init_value=0.0,
            peak_value=1e-4,
            warmup_steps=10000,
            decay_steps=2500000,
            end_value=1e-5)
        # self.opt = optax.adafactor(learning_rate=1e-4)
        self.opt = optax.adam(learning_rate=lr)
        self.train_state = train_state.TrainState.create(
            apply_fn=self.unet.apply,
            tx=self.opt,
            params=self.params['params']
        )
        self.imagen_state = ImagenState(train_state=self.train_state, sampler=self.lowres_scheduler, conditional_drop_prob=conditional_drop_prob)
        self.image_size = img_size
        self.p_train_step = pjit.pjit(train_step, in_axis_resources=P(), out_axis_resources=P(None, None))
        

    def get_key(self):
        self.random_state,key = jax.random.split(self.random_state)
        return key
    
    def sample(self, texts, attention):
        batch_size = texts.shape[0] 
        noise = jax.random.normal(self.get_key(), (batch_size, self.image_size, self.image_size, 3))
        return sample(self.imagen_state, noise, texts, attention, self.get_key())
    
    def train_step(self, image_batch, timestep, texts_batches=None, attention_batches=None):
        # shard prng key
        # image_batch_shape = (batch_size, image_size, image_size, 3)
        key = self.get_key()
        with maps.Mesh(self.mesh.devices, self.mesh.axis_names), nn_partitioning.axis_rules(nnp.DEFAULT_TPU_RULES):
            self.imagen_state, metrics = train_step(self.imagen_state, image_batch, timestep, texts_batches, attention_batches, key)
        return metrics

def compute_metrics(loss, logits):
    return {"loss": loss}

def test():
    imagen = Imagen()
    batch_size = 16
    while True:
        imagen.train_step(jnp.ones((batch_size, 64, 64, 3)), jnp.ones((batch_size,)) * 1, jnp.ones((batch_size, 256, 512)), jnp.ones((batch_size, 256)))

if __name__ == "__main__":
    test()
