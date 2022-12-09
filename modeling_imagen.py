from typing import Any, Dict, Tuple
from flax import linen as nn
import jax.numpy as jnp

from einops import rearrange, repeat, reduce, pack, unpack
from utils import exists, default
from layers import ResnetBlock, SinusoidalPositionEmbeddings, CrossEmbedLayer, TextConditioning, TransformerBlock, Downsample, Upsample, Attention, EinopsToAndFrom
from jax.experimental.pjit import PartitionSpec as P
import partitioning as nnp
from config import ListOrTuple, SingleOrList
from flax.linen import partitioning as nn_partitioning

with_sharding_constraint = nn_partitioning.with_sharding_constraint

class EfficentUNet(nn.Module):
    # config: Dict[str, Any]
    dim: int = 128
    dim_mults: Tuple[int, ...] = (1, 2, 4, 8)
    text_embed_dim: int = 512
    cond_dim: int = None  # default to dim
    channels: int = 3
    
    atten_dim_head: int = 32
    attn_heads: int = 4
    
    num_resnet_blocks: int = 3
    
    num_time_tokens: int = 2
    lowres_conditioning: bool = False
    max_token_len: int = 256

    strides: Tuple[int, int] = (2, 2)

    dtype: jnp.dtype = jnp.bfloat16

    @nn.compact
    def __call__(self, x, time, texts=None, attention_masks=None, condition_drop_prob=0.0, rng=None):
        
        x = with_sharding_constraint(x, P("batch", "height", "width", "embed"))
        texts = with_sharding_constraint(texts, P("batch", "seq", "embed"))
        
        time_conditioning_dim = self.dim * 4 * \
            (2 if self.lowres_conditioning else 1)
        cond_dim = default(self.cond_dim, self.dim)

        time_hidden = SinusoidalPositionEmbeddings(dim=self.dim)(time) # (b, 1, d)
        time_hidden = nnp.Dense(features=time_conditioning_dim, dtype=self.dtype, shard_axes={"kernel": ("embed_kernel", "mlp")})(time_hidden)
        time_hidden = nn.silu(time_hidden)

        t = nnp.Dense(features=time_conditioning_dim,
                     dtype=self.dtype, shard_axes={"kernel": ("embed_kernel", "mlp")})(time_hidden)
        
        t = with_sharding_constraint(t, P("batch", "embed"))

        time_tokens = nnp.Dense(cond_dim * self.num_time_tokens, dtype=self.dtype, shard_axes={"kernel": ("embed_kernel", "mlp")})(t)
        time_tokens = rearrange(time_tokens, 'b (r d) -> b r d', r=self.num_time_tokens)
        
        time_tokens = with_sharding_constraint(time_tokens, P("batch", "seq", "embed"))
        
        t, c = TextConditioning(cond_dim=cond_dim, time_cond_dim=time_conditioning_dim, max_token_length=self.max_token_len, cond_drop_prob=condition_drop_prob)(texts, attention_masks, t, time_tokens, rng)
        # TODO: add lowres conditioning
        
        t = with_sharding_constraint(t, P("batch", "embed"))
        c = with_sharding_constraint(c, P("batch", "embed"))
                
        x = CrossEmbedLayer(dim=self.dim,
                            kernel_sizes=(3, 7, 15), stride=1, dtype=self.dtype)(x)
        x = with_sharding_constraint(x, P("batch", "height", "width", "embed"))
        
        init_conv_residual = x
        # downsample
        hiddens = []
        for dim_mult in self.dim_mults:
            x = Downsample(dim=self.dim * dim_mult)(x)
            x = ResnetBlock(dim=self.dim * dim_mult, dtype=self.dtype)(x, t, c)
            for _ in range(self.num_resnet_blocks):
                x = ResnetBlock(dim=self.dim * dim_mult, dtype=self.dtype)(x)
                x = with_sharding_constraint(x, P("batch", "height", "width", "embed"))
                hiddens.append(x)
            x = TransformerBlock(dim=self.dim * dim_mult, heads=self.attn_heads, dim_head=self.atten_dim_head, dtype=self.dtype)(x)
            x = with_sharding_constraint(x, P("batch", "height", "width", "embed"))
            hiddens.append(x)
        x = ResnetBlock(dim=self.dim * self.dim_mults[-1], dtype=self.dtype)(x, t, c)
        x = EinopsToAndFrom(Attention(self.dim * self.dim_mults[-1]), 'b h w c', 'b (h w) c')(x)
        x = ResnetBlock(dim=self.dim * self.dim_mults[-1], dtype=self.dtype)(x, t, c)
        
        # Upsample
        add_skip_connection = lambda x: jnp.concatenate([x, hiddens.pop()], axis=-1)
        for dim_mult in reversed(self.dim_mults):
            x = add_skip_connection(x)
            x = ResnetBlock(dim=self.dim * dim_mult, dtype=self.dtype)(x, t, c)
            for _ in range(self.num_resnet_blocks):
                x = add_skip_connection(x)
                x = with_sharding_constraint(x, P("batch", "height", "width", "embed"))
                x = ResnetBlock(dim=self.dim * dim_mult, dtype=self.dtype)(x)
                x = with_sharding_constraint(x, P("batch", "height", "width", "embed"))
            
            x = TransformerBlock(dim=self.dim * dim_mult, heads=self.attn_heads, dim_head=self.atten_dim_head, dtype=self.dtype)(x)
            x = Upsample(dim=self.dim * dim_mult)(x)
        
        x = jnp.concatenate([x, init_conv_residual], axis=-1)
        
        x = ResnetBlock(dim=self.dim, dtype=self.dtype)(x, t, c)
            
        # x = nn.Dense(features=3, dtype=self.dtype)(x)
        x = nn.Conv(features=3, kernel_size=(3, 3), strides=1, dtype=self.dtype, padding=1)(x)
        return x    