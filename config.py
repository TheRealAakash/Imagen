from enum import Enum
from typing import List, Iterable, Optional, Union, Tuple, Dict, Any
import jax.numpy as jnp
from pydantic import BaseModel

def ListOrTuple(inner_type):
    return Union[List[inner_type], Tuple[inner_type]]


def SingleOrList(inner_type):
    return Union[inner_type, ListOrTuple(inner_type)]


class UnetConfig(BaseModel):
    dim:                       int = 128
    dim_mults:                 ListOrTuple(int) = (1, 2, 4, 8)
    cond_dim:                  int = 128

    time_conditiong_dim:       int = 512  # dim * 4 (* 2 if lowres_conditioning)

    num_time_tokens:           int = 2
    max_token_len:             int = 256
    token_embedding_dim:       int = 512

    channels:                  int = 3

    dim_heads:                 int = 32
    num_heads:                 int = 4
    ff_mult:                   int = 2

    num_resnet_blocks:         int = 3

    lowres_conditioning:       bool = False

    strides: Tuple[int, int] = (2, 2)
    
    scheduler:                 str = 'cosine'

    dtype:                     Any = jnp.bfloat16


class ImagenConfig(BaseModel):
    unets:                  ListOrTuple(UnetConfig) = [UnetConfig(dim=128, dim_mults=(1, 2, 4, 8), scheduler="cosine")]
    image_sizes:            ListOrTuple(int) = (64,)
    timesteps:              int = 1000

    text_encoder_name:      str = "t5-small"

    channels:               int = 3
    loss_type:              str = 'l2'
    cond_drop_prob:         float = 0.5
    
    batch_size:             int = 128
