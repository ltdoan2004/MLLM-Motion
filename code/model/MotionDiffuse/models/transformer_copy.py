"""
Copyright 2021 S-Lab
"""

from cv2 import norm
import torch
import torch.nn.functional as F
from torch import layer_norm, nn
import numpy as np
import clip

import math


def timestep_embedding(timesteps, dim, max_period=10000):
    """
    Create sinusoidal timestep embeddings.
    :param timesteps: a 1-D Tensor of N indices, one per batch element.
                      These may be fractional.
    :param dim: the dimension of the output.
    :param max_period: controls the minimum frequency of the embeddings.
    :return: an [N x dim] Tensor of positional embeddings.
    """
    half = dim // 2
    freqs = torch.exp(
        -math.log(max_period) * torch.arange(start=0, end=half, dtype=torch.float32) / half
    ).to(device='cuda')
    args = timesteps[:, None].float() * freqs[None]
    embedding = torch.cat([torch.cos(args), torch.sin(args)], dim=-1)
    if dim % 2:
        embedding = torch.cat([embedding, torch.zeros_like(embedding[:, :1])], dim=-1)
    return embedding


def set_requires_grad(nets, requires_grad=False):
    """Set requies_grad for all the networks.

    Args:
        nets (nn.Module | list[nn.Module]): A list of networks or a single
            network.
        requires_grad (bool): Whether the networks require gradients or not
    """
    if not isinstance(nets, list):
        nets = [nets]
    for net in nets:
        if net is not None:
            for param in net.parameters():
                param.requires_grad = requires_grad


def zero_module(module):
    """
    Zero out the parameters of a module and return it.
    """
    for p in module.parameters():
        p.detach().zero_()
    return module


class StylizationBlock(nn.Module):

    def __init__(self, latent_dim, time_embed_dim, dropout):
        super().__init__()
        self.emb_layers = nn.Sequential(
            nn.SiLU(),
            nn.Linear(time_embed_dim, 2 * latent_dim),
        )
        self.norm = nn.LayerNorm(latent_dim)
        self.out_layers = nn.Sequential(
            nn.SiLU(),
            nn.Dropout(p=dropout),
            zero_module(nn.Linear(latent_dim, latent_dim)),
        )

    def forward(self, h, emb):
        """
        h: B, T, D
        emb: B, D
        """
        # B, 1, 2D
        emb_out = self.emb_layers(emb).unsqueeze(1)
        # scale: B, 1, D / shift: B, 1, D
        scale, shift = torch.chunk(emb_out, 2, dim=2)
        h = self.norm(h) * (1 + scale) + shift
        h = self.out_layers(h)
        return h


class LinearTemporalSelfAttention(nn.Module):

    def __init__(self, seq_len, latent_dim, num_head, dropout, time_embed_dim):
        super().__init__()
        self.num_head = num_head
        self.norm = nn.LayerNorm(latent_dim)
        self.query = nn.Linear(latent_dim, latent_dim)
        self.key = nn.Linear(latent_dim, latent_dim)
        self.value = nn.Linear(latent_dim, latent_dim)
        self.dropout = nn.Dropout(dropout)
        self.proj_out = StylizationBlock(latent_dim, time_embed_dim, dropout)
    
    # def forward(self, x, emb, src_mask):
    #     """
    #     x: B, T, D
    #     """
    #     # print('------------')
    #     # print(src_mask.shape)
    #     # print(emb.shape)
    #     # print(x.shape)
    #     # x = F.adaptive_avg_pool1d(x.transpose(1, 2), 60).transpose(1, 2)
    #     # print(x.shape)
    #     B, T, D = x.shape
    #     H = self.num_head
    #     # print(H)
    #     # B, T, D
    #     query = self.query(self.norm(x))
    #     # print('query' + str(query.shape))
    #     # B, T, D
    #     key = (self.key(self.norm(x)) + (1 - src_mask) * -1000000)
    #     # print('key' + str(key.shape))
    #     query = F.softmax(query.view(B, T, H, -1), dim=-1)
    #     # print('query 2' + str(query.shape))
    #     key = F.softmax(key.view(B, T, H, -1), dim=1)
    #     # print('key 2' + str(key.shape))
    #     # B, T, H, HD
    #     value = (self.value(self.norm(x)) * src_mask).view(B, T, H, -1)
    #     # print('value' + str(key.shape))
    #     # B, H, HD, HD
    #     attention = torch.einsum('bnhd,bnhl->bhdl', key, value)
    #     # print('attention' + str(attention.shape))
    #     y = torch.einsum('bnhd,bhdl->bnhl', query, attention).reshape(B, T, D)
    #     y = x + self.proj_out(y, emb)
    #     return y

    # def forward(self, x, emb, src_mask):
    #     B, T, D = x.shape
    #     H = self.num_head
    #     head_dim = D // H  # Ensure D is divisible by H

    #     norm_x = self.norm(x)  # Normalization done once for efficiency
    #     query = self.query(norm_x).view(B, T, H, head_dim)
    #     key = self.key(norm_x).view(B, T, H, head_dim)
    #     value = self.value(norm_x).view(B, T, H, head_dim)

    #     # Apply mask to keys
    #     key += (1 - src_mask.unsqueeze(-1)) * -1000000.0

    #     # Softmax applied correctly for queries and keys
    #     query = F.softmax(query, dim=-1)  # Softmax over head_dim
    #     key = F.softmax(key, dim=2)  # Softmax over T

    #     # Attention computation
    #     attention = torch.einsum('bthd,bthl->bhdl', key, value)
    #     y = torch.einsum('bthd,bhdl->bthl', query, attention).reshape(B, T, D)

    #     # Output projection and residual connection
    #     y = x + self.proj_out(y, emb)

    #     return y
    
    def forward(self, x, emb, src_mask):
        """
        x: B, T, D
        """
        B, T, D = x.shape
        H = self.num_head
        # B, T, D
        query = self.query(self.norm(x))
        # B, T, D
        key = (self.key(self.norm(x)) + (1 - src_mask) * -1000000)
        query = F.softmax(query.view(B, T, H, -1), dim=-1)
        key = F.softmax(key.view(B, T, H, -1), dim=1)
        # B, T, H, HD
        value = (self.value(self.norm(x)) * src_mask).view(B, T, H, -1)
        # B, H, HD, HD
        attention = torch.einsum('bnhd,bnhl->bhdl', key, value)
        y = torch.einsum('bnhd,bhdl->bnhl', query, attention).reshape(B, T, D)
        y = x + self.proj_out(y, emb)
        return y
    

class LinearTemporalCrossAttention(nn.Module):

    def __init__(self, seq_len, latent_dim, text_latent_dim, num_head, dropout, time_embed_dim):
        super().__init__()
        self.num_head = num_head
        self.norm = nn.LayerNorm(latent_dim)
        self.text_norm = nn.LayerNorm(text_latent_dim)
        self.query = nn.Linear(latent_dim, latent_dim)
        self.key = nn.Linear(text_latent_dim, latent_dim)
        self.value = nn.Linear(text_latent_dim, latent_dim)
        self.dropout = nn.Dropout(dropout)
        self.proj_out = StylizationBlock(latent_dim, time_embed_dim, dropout)
    
    def forward(self, x, xf, emb):
        """
        x: B, T, D
        xf: B, N, L
        """
        B, T, D = x.shape
        N = xf.shape[1]
        H = self.num_head
        # B, T, D
        query = self.query(self.norm(x))
        # B, N, D
        key = self.key(self.text_norm(xf))
        query = F.softmax(query.view(B, T, H, -1), dim=-1)
        key = F.softmax(key.view(B, N, H, -1), dim=1)
        # B, N, H, HD
        value = self.value(self.text_norm(xf)).view(B, N, H, -1)
        # B, H, HD, HD
        attention = torch.einsum('bnhd,bnhl->bhdl', key, value)
        y = torch.einsum('bnhd,bhdl->bnhl', query, attention).reshape(B, T, D)
        y = x + self.proj_out(y, emb)
        return y

class FFN(nn.Module):

    def __init__(self, latent_dim, ffn_dim, dropout, time_embed_dim):
        super().__init__()
        self.linear1 = nn.Linear(latent_dim, ffn_dim)
        self.linear2 = zero_module(nn.Linear(ffn_dim, latent_dim))
        self.activation = nn.GELU()
        self.dropout = nn.Dropout(dropout)
        self.proj_out = StylizationBlock(latent_dim, time_embed_dim, dropout)

    def forward(self, x, emb):
        y = self.linear2(self.dropout(self.activation(self.linear1(x))))
        y = x + self.proj_out(y, emb)
        return y
    

class LinearTemporalDiffusionTransformerDecoderLayer(nn.Module):

    def __init__(self,
                 seq_len=60,
                 latent_dim=32,
                 text_latent_dim=512,
                 time_embed_dim=128,
                 ffn_dim=256,
                 num_head=4,
                 dropout=0.1):
        super().__init__()
        self.sa_block = LinearTemporalSelfAttention(
            seq_len, latent_dim, num_head, dropout, time_embed_dim)
        self.ca_block = LinearTemporalCrossAttention(
            seq_len, latent_dim, text_latent_dim, num_head, dropout, time_embed_dim)
        self.ffn = FFN(latent_dim, ffn_dim, dropout, time_embed_dim)

    def forward(self, x, xf, emb, src_mask):
        x = self.sa_block(x, emb, src_mask)
        x = self.ca_block(x, xf, emb)
        x = self.ffn(x, emb)
        return x

class TemporalSelfAttention(nn.Module):

    def __init__(self, seq_len, latent_dim, num_head, dropout, time_embed_dim):
        super().__init__()
        self.num_head = num_head
        self.norm = nn.LayerNorm(latent_dim)
        self.query = nn.Linear(latent_dim, latent_dim)
        self.key = nn.Linear(latent_dim, latent_dim)
        self.value = nn.Linear(latent_dim, latent_dim)
        self.dropout = nn.Dropout(dropout)
        self.proj_out = StylizationBlock(latent_dim, time_embed_dim, dropout)
    
    def forward(self, x, emb, src_mask):
        """
        x: B, T, D
        """
        B, T, D = x.shape
        H = self.num_head
        # B, T, 1, D
        query = self.query(self.norm(x)).unsqueeze(2)
        # B, 1, T, D
        key = self.key(self.norm(x)).unsqueeze(1)
        query = query.view(B, T, H, -1)
        key = key.view(B, T, H, -1)
        # B, T, T, H
        attention = torch.einsum('bnhd,bmhd->bnmh', query, key) / math.sqrt(D // H)
        attention = attention + (1 - src_mask.unsqueeze(-1)) * -100000
        weight = self.dropout(F.softmax(attention, dim=2))
        value = self.value(self.norm(x)).view(B, T, H, -1)
        y = torch.einsum('bnmh,bmhd->bnhd', weight, value).reshape(B, T, D)
        y = x + self.proj_out(y, emb)
        return y

class TemporalCrossAttention(nn.Module):

    def __init__(self, seq_len, latent_dim, text_latent_dim, num_head, dropout, time_embed_dim):
        super().__init__()
        self.num_head = num_head
        self.norm = nn.LayerNorm(latent_dim)
        self.text_norm = nn.LayerNorm(text_latent_dim)
        self.query = nn.Linear(latent_dim, latent_dim)
        self.key = nn.Linear(text_latent_dim, latent_dim)
        self.value = nn.Linear(text_latent_dim, latent_dim)
        self.dropout = nn.Dropout(dropout)
        self.proj_out = StylizationBlock(latent_dim, time_embed_dim, dropout)
    
    def forward(self, x, xf, emb):
        """
        x: B, T, D
        xf: B, N, L
        """
        B, T, D = x.shape
        N = xf.shape[1]
        H = self.num_head
        # B, T, 1, D
        query = self.query(self.norm(x)).unsqueeze(2)
        # B, 1, N, D
        key = self.key(self.text_norm(xf)).unsqueeze(1)
        query = query.view(B, T, H, -1)
        key = key.view(B, N, H, -1)
        # B, T, N, H
        attention = torch.einsum('bnhd,bmhd->bnmh', query, key) / math.sqrt(D // H)
        weight = self.dropout(F.softmax(attention, dim=2))
        value = self.value(self.text_norm(xf)).view(B, N, H, -1)
        y = torch.einsum('bnmh,bmhd->bnhd', weight, value).reshape(B, T, D)
        y = x + self.proj_out(y, emb)
        return y

class TemporalDiffusionTransformerDecoderLayer(nn.Module):

    def __init__(self,
                 seq_len=60,
                 latent_dim=32,
                 text_latent_dim=512,
                 time_embed_dim=128,
                 ffn_dim=256,
                 num_head=4,
                 dropout=0.1):
        super().__init__()
        self.sa_block = TemporalSelfAttention(
            seq_len, latent_dim, num_head, dropout, time_embed_dim)
        self.ca_block = TemporalCrossAttention(
            seq_len, latent_dim, text_latent_dim, num_head, dropout, time_embed_dim)
        self.ffn = FFN(latent_dim, ffn_dim, dropout, time_embed_dim)

    def forward(self, x, xf, emb, src_mask):
        x = self.sa_block(x, emb, src_mask)
        x = self.ca_block(x, xf, emb)
        x = self.ffn(x, emb)
        return x
import torch
import torch.nn as nn

import torch
import torch.nn as nn

# class CrossAttentionFusionTransformer(nn.Module):
#     def __init__(self, input_dim, model_dim, nhead, num_layers, output_dim):
#         super(CrossAttentionFusionTransformer, self).__init__()
        
#         # Projection layers to project each input to model_dim
#         self.proj = nn.Linear(input_dim, model_dim)
        
#         # Cross-attention mechanism for fusion
#         self.cross_attention_1 = nn.MultiheadAttention(embed_dim=model_dim, num_heads=nhead)
#         self.cross_attention_2 = nn.MultiheadAttention(embed_dim=model_dim, num_heads=nhead)
#         self.cross_attention_3 = nn.MultiheadAttention(embed_dim=model_dim, num_heads=nhead)
        
#         # Transformer encoder to refine the fused representation
#         self.transformer = nn.TransformerEncoder(
#             nn.TransformerEncoderLayer(d_model=model_dim, nhead=nhead),
#             num_layers=num_layers
#         )
        
#         # Additional feed-forward network for further fusion
#         self.fusion_ffn = nn.Sequential(
#             nn.Linear(model_dim * 4, model_dim * 2),
#             nn.ReLU(),
#             nn.Linear(model_dim * 2, model_dim),
#             nn.ReLU()
#         )
        
#         # Output projection to get the desired output dimension [batch_size, output_dim]
#         self.output_proj = nn.Linear(model_dim, output_dim)

#     def forward(self, tensor1, tensor2, tensor3):
#         # tensor1, tensor2, tensor3: [batch_size, 2048]
        
#         # Project tensors to model_dim
#         tensor1_proj = self.proj(tensor1)  # Shape: [batch_size, model_dim]
#         tensor2_proj = self.proj(tensor2)  # Shape: [batch_size, model_dim]
#         tensor3_proj = self.proj(tensor3)  # Shape: [batch_size, model_dim]
        
#         # Prepare inputs for cross-attention (sequence_length, batch_size, model_dim)
#         tensor1_proj = tensor1_proj.unsqueeze(0)  # Shape: [1, batch_size, model_dim]
#         tensor2_proj = tensor2_proj.unsqueeze(0)  # Shape: [1, batch_size, model_dim]
#         tensor3_proj = tensor3_proj.unsqueeze(0)  # Shape: [1, batch_size, model_dim]
        
#         # Cross-attention to fuse tensor1 and tensor2
#         attn_output_1, _ = self.cross_attention_1(tensor1_proj, tensor2_proj, tensor2_proj)  # Shape: [1, batch_size, model_dim]
#         # Cross-attention to fuse the result with tensor3
#         attn_output_2, _ = self.cross_attention_2(attn_output_1, tensor3_proj, tensor3_proj)  # Shape: [1, batch_size, model_dim]
#         # Cross-attention to further refine fusion between all three tensors
#         attn_output_3, _ = self.cross_attention_3(attn_output_2, tensor1_proj, tensor1_proj)  # Shape: [1, batch_size, model_dim]
        
#         # Concatenate the fused representations
#         combined_input = torch.cat((tensor1_proj, tensor2_proj, tensor3_proj, attn_output_3), dim=0)  # Shape: [4, batch_size, model_dim]
        
#         # Apply transformer encoder to the combined representation
#         transformer_output = self.transformer(combined_input)  # Shape: [4, batch_size, model_dim]
        
#         # Aggregate transformer output
#         aggregated_output = transformer_output.mean(dim=0)  # Shape: [batch_size, model_dim]
        
#         # Further fusion using feed-forward network
#         fusion_input = torch.cat((tensor1_proj.squeeze(0), tensor2_proj.squeeze(0), tensor3_proj.squeeze(0), aggregated_output), dim=-1)  # Shape: [batch_size, model_dim * 4]
#         fused_output = self.fusion_ffn(fusion_input)  # Shape: [batch_size, model_dim]
        
#         # Project fused output to the desired output dimension
#         final_output = self.output_proj(fused_output)  # Shape: [batch_size, output_dim]
        
#         return final_output
# import torch

class FusionExpert(nn.Module):
    def __init__(self, input_dim, model_dim, nhead, num_layers, dropout=0.1):
        super(FusionExpert, self).__init__()

        # Projection layers to project each input to model_dim
        self.proj = nn.Linear(input_dim, model_dim)

        # Cross-attention mechanisms for fusion
        self.cross_attention_1 = nn.MultiheadAttention(embed_dim=model_dim, num_heads=nhead, dropout=dropout)
        self.cross_attention_2 = nn.MultiheadAttention(embed_dim=model_dim, num_heads=nhead, dropout=dropout)

        # Layer Normalization
        self.layer_norm1 = nn.LayerNorm(model_dim)
        self.layer_norm2 = nn.LayerNorm(model_dim)
        self.layer_norm3 = nn.LayerNorm(model_dim)

        # Dropout layer
        self.dropout = nn.Dropout(dropout)

        # Transformer encoder to refine the fused representation
        encoder_layer = nn.TransformerEncoderLayer(d_model=model_dim, nhead=nhead, dropout=dropout)
        self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)

        # Additional feed-forward network for further fusion
        self.fusion_ffn = nn.Sequential(
            nn.Linear(model_dim * 3, model_dim * 2),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(model_dim * 2, model_dim),
            nn.ReLU()
        )

    def forward(self, tensor1, tensor2):
        # tensor1, tensor2: [batch_size, input_dim]

        # Project tensors to model_dim
        tensor1_proj = self.proj(tensor1)  # Shape: [batch_size, model_dim]
        tensor2_proj = self.proj(tensor2)  # Shape: [batch_size, model_dim]

        # Apply LayerNorm to projections
        tensor1_proj = self.layer_norm1(tensor1_proj)
        tensor2_proj = self.layer_norm1(tensor2_proj)

        # Prepare inputs for cross-attention (sequence_length, batch_size, model_dim)
        tensor1_proj = tensor1_proj.unsqueeze(0)  # Shape: [1, batch_size, model_dim]
        tensor2_proj = tensor2_proj.unsqueeze(0)  # Shape: [1, batch_size, model_dim]

        # Cross-attention to fuse tensor1 and tensor2 with residual connections
        attn_output_1, _ = self.cross_attention_1(tensor1_proj, tensor2_proj, tensor2_proj)
        attn_output_1 = self.dropout(attn_output_1)
        attn_output_1 = attn_output_1 + tensor1_proj  # Residual connection
        attn_output_1 = self.layer_norm2(attn_output_1)

        # Cross-attention to further refine fusion between both tensors with residual connections
        attn_output_2, _ = self.cross_attention_2(attn_output_1, tensor1_proj, tensor1_proj)
        attn_output_2 = self.dropout(attn_output_2)
        attn_output_2 = attn_output_2 + attn_output_1  # Residual connection
        attn_output_2 = self.layer_norm3(attn_output_2)

        # Concatenate the fused representations
        combined_input = torch.cat((tensor1_proj, tensor2_proj, attn_output_2), dim=0)  # Shape: [3, batch_size, model_dim]

        # Apply transformer encoder to the combined representation
        transformer_output = self.transformer(combined_input)  # Shape: [3, batch_size, model_dim]

        # Aggregate transformer output
        aggregated_output = transformer_output.mean(dim=0)  # Shape: [batch_size, model_dim]

        # Further fusion using feed-forward network with residual connection
        fusion_input = torch.cat((
            tensor1_proj.squeeze(0),
            tensor2_proj.squeeze(0),
            aggregated_output
        ), dim=-1)  # Shape: [batch_size, model_dim * 3]

        fused_output = self.fusion_ffn(fusion_input)  # Shape: [batch_size, model_dim]
        fused_output = self.dropout(fused_output)

        return fused_output  # Shape: [batch_size, model_dim]

class GatingNetwork(nn.Module):
    def __init__(self, input_dim, num_experts):
        super(GatingNetwork, self).__init__()
        self.gate = nn.Sequential(
            nn.Linear(input_dim * 2, 128),
            nn.ReLU(),
            nn.Linear(128, num_experts),
            nn.Softmax(dim=-1)
        )
    
    def forward(self, tensor1, tensor2):
        # Concatenate inputs
        gate_input = torch.cat((tensor1, tensor2), dim=-1)  # Shape: [batch_size, input_dim * 2]
        gating_weights = self.gate(gate_input)  # Shape: [batch_size, num_experts]
        return gating_weights

class MoEFusionTransformer(nn.Module):
    def __init__(self, input_dim, model_dim, nhead, num_layers, output_dim, num_experts, dropout=0.1):
        super(MoEFusionTransformer, self).__init__()

        self.num_experts = num_experts

        # Define experts
        self.experts = nn.ModuleList([
            FusionExpert(input_dim, model_dim, nhead, num_layers, dropout=dropout)
            for _ in range(num_experts)
        ])

        # Define gating network
        self.gating_network = GatingNetwork(input_dim, num_experts)

        # Output projection to get the desired output dimension
        self.output_proj = nn.Linear(model_dim, output_dim)

    def forward(self, tensor1, tensor2):
        # tensor1, tensor2: [batch_size, input_dim]

        # Get gating weights
        gating_weights = self.gating_network(tensor1, tensor2)  # Shape: [batch_size, num_experts]

        # Compute outputs from all experts
        expert_outputs = []
        for expert in self.experts:
            expert_output = expert(tensor1, tensor2)  # Shape: [batch_size, model_dim]
            expert_outputs.append(expert_output.unsqueeze(1))  # Shape: [batch_size, 1, model_dim]

        # Stack expert outputs: Shape: [batch_size, num_experts, model_dim]
        expert_outputs = torch.cat(expert_outputs, dim=1)

        # Combine expert outputs using gating weights
        gating_weights = gating_weights.unsqueeze(-1)  # Shape: [batch_size, num_experts, 1]
        fused_output = torch.sum(gating_weights * expert_outputs, dim=1)  # Shape: [batch_size, model_dim]

        # Project fused output to the desired output dimension
        final_output = self.output_proj(fused_output)  # Shape: [batch_size, output_dim]

        return final_output


class MotionTransformer(nn.Module):
    def __init__(self,
                 input_feats,
                 num_frames=240,
                 latent_dim=512,
                 ff_size=1024,
                 num_layers=8,
                 num_heads=8,
                 dropout=0,
                 activation="gelu", 
                 num_text_layers=4,
                 text_latent_dim=256,
                 text_ff_size=2048,
                 text_num_heads=4,
                 no_clip=False,
                 no_eff=False,
                 **kargs):
        super().__init__()
        

        # Xavier initialization for the weights
        limit = torch.sqrt(torch.tensor(6.0) / (2048 + 2048))
        self.weights_scores_promt = torch.empty(1, 2048).uniform_(-limit, limit)
        self.weights_scores_signals = torch.empty(1, 2048).uniform_(-limit, limit)
        # self.fusion_model = CrossAttentionFusionTransformer(input_dim=2048, model_dim=512, nhead=8, num_layers=2, output_dim=2048)
        self.fusion_model = MoEFusionTransformer(input_dim=2048,model_dim=512,nhead=8,num_layers=6,output_dim=2048,num_experts=4,dropout=0.1 )
        self.num_frames = num_frames
        self.latent_dim = latent_dim
        self.ff_size = ff_size
        self.num_layers = num_layers
        self.num_heads = num_heads
        self.dropout = dropout
        self.activation = activation  
        self.input_feats = input_feats
        self.time_embed_dim = latent_dim * 4
        self.sequence_embedding = nn.Parameter(torch.randn(num_frames, latent_dim))

        # Text Transformer
        self.clip, _ = clip.load('ViT-B/32', "cpu")
        if no_clip:
            self.clip.initialize_parameters()
        else:
            set_requires_grad(self.clip, False)
        if text_latent_dim != 512:
            self.text_pre_proj = nn.Linear(512, text_latent_dim)
            self.text_pre_proj_new = nn.Linear(512, text_latent_dim)
        else:
            self.text_pre_proj = nn.Identity()
            self.text_pre_proj_new = nn.Identity()
        textTransEncoderLayer = nn.TransformerEncoderLayer(
            d_model=text_latent_dim,
            nhead=text_num_heads,
            dim_feedforward=text_ff_size,
            dropout=dropout,
            activation=activation)
        textTransEncoderLayer_new = nn.TransformerEncoderLayer(
            d_model=text_latent_dim,
            nhead=text_num_heads,
            dim_feedforward=text_ff_size,
            dropout=dropout,
            activation=activation)
        self.textTransEncoder = nn.TransformerEncoder(
            textTransEncoderLayer,
            num_layers=num_text_layers)
        self.text_ln = nn.LayerNorm(text_latent_dim)
        self.text_proj = nn.Sequential(
            nn.Linear(text_latent_dim, self.time_embed_dim)
        )
        # self.textTransEncoder_new = nn.TransformerEncoder(
        #     textTransEncoderLayer_new,
        #     num_layers=num_text_layers)
        # self.text_ln_new = nn.LayerNorm(text_latent_dim)
        # self.text_proj_new = nn.Sequential(
        #     nn.Linear(text_latent_dim, self.time_embed_dim)
        # )

        # Input Embedding
        self.joint_embed = nn.Linear(self.input_feats, self.latent_dim)

        # self.linear = nn.Linear(768,512) #convert dimension from the output of nextgpt
        # nn.init.xavier_uniform_(self.linear.weight)
        # nn.init.zeros_(self.linear.bias)

        self.time_embed = nn.Sequential(
            nn.Linear(self.latent_dim, self.time_embed_dim),
            nn.SiLU(),
            nn.Linear(self.time_embed_dim, self.time_embed_dim),
        )
        # self.time_embed1 = nn.Sequential(
        #     nn.Linear(self.latent_dim, self.time_embed_dim),
        #     nn.SiLU(),
        #     nn.Linear(self.time_embed_dim, self.time_embed_dim),
        # )
        # nn.init.xavier_uniform_(self.time_embed1.weight)
        # nn.init.zeros_(self..bias)
        self.temporal_decoder_blocks = nn.ModuleList()
        for i in range(num_layers):
            if no_eff:
                self.temporal_decoder_blocks.append(
                    TemporalDiffusionTransformerDecoderLayer(
                        seq_len=num_frames,
                        latent_dim=latent_dim,
                        text_latent_dim=text_latent_dim,
                        time_embed_dim=self.time_embed_dim,
                        ffn_dim=ff_size,
                        num_head=num_heads,
                        dropout=dropout
                    )
                )
            else:
                self.temporal_decoder_blocks.append(
                    LinearTemporalDiffusionTransformerDecoderLayer(
                        seq_len=num_frames,
                        latent_dim=latent_dim,
                        text_latent_dim=text_latent_dim,
                        time_embed_dim=self.time_embed_dim,
                        ffn_dim=ff_size,
                        num_head=num_heads,
                        dropout=dropout
                    )
                )
        
        # Output Module
        self.out = zero_module(nn.Linear(self.latent_dim, self.input_feats))
    def weighting_scores(self, input_vector, weights):
        """
        Apply weighting scores to an input vector.

        Parameters:
        - input_vector: torch tensor of shape [32, 2048]
        - weights: torch tensor of shape [2048] or [1, 2048]

        Returns:
        - weighted_vector: torch tensor of shape [32, 2048]
        """
        # Ensure weights are on the same device as input_vector
        weights = weights.to(input_vector.device)

        # Apply the weights - broadcasting allows element-wise multiplication across the batch
        weighted_vector = input_vector * weights  # Shape: [32, 2048]

        # Apply a non-linear activation to enhance feature representation
        weighted_vector = torch.relu(weighted_vector)

        # Optionally, you can normalize each vector in the batch
        norms = torch.norm(weighted_vector, p=2, dim=1, keepdim=True)  # Compute norms for each vector in the batch
        weighted_vector = weighted_vector / (norms + 1e-6)  # Normalize each vector to have unit length, adding epsilon to prevent division by zero

        # Apply a learnable scaling factor
        scaling_factor = nn.Parameter(torch.ones(1, 2048).to(input_vector.device))
        weighted_vector = weighted_vector * scaling_factor

        return weighted_vector
    


    # def encode_text(self, text, device):
    #     with torch.no_grad():
    #         text = clip.tokenize(text, truncate=True).to(device)
    #         # print(text)
    #         x = self.clip.token_embedding(text).type(self.clip.dtype)  # [batch_size, n_ctx, d_model]
    #         # print(x.shape)
    #         x = x + self.clip.positional_embedding.type(self.clip.dtype)
    #         print(x.shape)
    #         x = x.permute(1, 0, 2)  # NLD -> LND
    #         # print(x.shape)
    #         x = self.clip.transformer(x)
    #         x = self.clip.ln_final(x).type(self.clip.dtype)

    #     # T, B, D
    #     x = self.text_pre_proj(x)
    #     xf_out = self.textTransEncoder(x)
    #     xf_out = self.text_ln(xf_out)
    #     xf_out_pooled = torch.mean(xf_out, dim=0)
    #     xf_proj = self.text_proj(xf_out_pooled)
    #     # print(xf_out.shape)
    #     # print(xf_out.shape[1])
    #     # print(text.argmax(dim=-1))
    #     # print(torch.arange(xf_out.shape[1]))
    #     # print(xf_out[text.argmax(dim=-1), torch.arange(xf_out.shape[1])].shape)
    #     # xf_proj = self.text_proj(xf_out[text.argmax(dim=-1), torch.arange(xf_out.shape[1])])
    #     # B, T, D
    #     xf_out = xf_out.permute(1, 0, 2)
    #     return xf_proj, xf_out
        # return x
    
    def encode_promt(self, text, device):
        with torch.no_grad():
            text = clip.tokenize(text, truncate=True).to(device)
            # print(text)
            x = self.clip.token_embedding(text).type(self.clip.dtype)  # [batch_size, n_ctx, d_model]
            # print(x.shape)
            x = x + self.clip.positional_embedding.type(self.clip.dtype)
            x = x.permute(1, 0, 2)  # NLD -> LND
            # print(x.shape)
            x = self.clip.transformer(x)
            x = self.clip.ln_final(x).type(self.clip.dtype)
        # x = self.text_pre_proj_new(x)
        # x = self.textTransEncoder_new(x)
        # x = self.text_ln_new(x)
        # x = torch.mean(x, dim=0) #[1, 256]
        # xf_proj = self.text_proj_new(x)
        x = self.text_pre_proj(x)
        xf_out = self.textTransEncoder(x)
        xf_out = self.text_ln(xf_out)
        xf_out_pooled = torch.mean(xf_out, dim=0)
        xf_proj = self.text_proj(xf_out_pooled)
        return xf_proj
            
    def encode_text(self, gen_emb, device):
        # print('encode text')
        # print(self.clip.positional_embedding.type(self.clip.dtype).shape)
        # print(gen_emb.shape)
        # gen_emb.to(device)
        # # Convert gen_emb to the same dtype as the linear layer expects
        gen_emb = gen_emb.to(device).type(self.clip.dtype)

        # gen_emb = self.linear(gen_emb)

        x = gen_emb + self.clip.positional_embedding.to(device).type(self.clip.dtype)
        # x = gen_emb.to(device)
        x = x.permute(1, 0, 2)  # NLD -> LND
    
        x = self.clip.transformer(x)
        x = self.clip.ln_final(x)

        x = self.text_pre_proj(x)
        xf_out = self.textTransEncoder(x)
        xf_out = self.text_ln(xf_out)
        xf_out_pooled = torch.mean(xf_out, dim=0)
        xf_proj = self.text_proj(xf_out_pooled)
        # xf_proj = self.text_proj(xf_out[torch.tensor([5]), torch.arange(xf_out.shape[1])])
        # Q = torch.nn.Linear(256, 256).to(device)(xf_out)  # Query
        # K = torch.nn.Linear(256, 256).to(device)(xf_out)  # Key
        # V = torch.nn.Linear(256, 256).to(device)(xf_out)  # Value

        # scores = torch.matmul(Q, K.transpose(-2, -1)) / (256 ** 0.5)  # Kích thước scores: [77, 1, 77]

        # attention_weights = F.softmax(scores, dim=-1)  # Kích thước: [77, 1, 77]

        # context = torch.matmul(attention_weights, V)  # Kích thước: [77, 1, 256]

        # xf_proj = context.sum(dim=0)  # Kích thước: [1, 256]
        # xf_proj = self.text_proj(xf_proj)
        xf_out = xf_out.permute(1, 0, 2)
        return xf_proj, xf_out



    def generate_src_mask(self, T, length):
        B = len(length)
        src_mask = torch.ones(B, T)
        for i in range(B):
            for j in range(length[i], T):
                src_mask[i, j] = 0
        return src_mask

    # def forward(self, x, timesteps, length=None, text=None, xf_proj=None, xf_out=None):
    #     """
    #     x: B, T, D
    #     """
    #     # print(length)
    #     B, T = x.shape[0], x.shape[1]
    #     # print('---------------')
    #     # print('xf_proj shape if not none: ' + str(xf_proj.shape))
    #     # print('xf_out shape if not none: ' + str(xf_out.shape))
    #     if xf_proj is None or xf_out is None:
    #         # xf_proj, xf_out = self.encode_text(text, x.device)
    #         xf_proj, xf_out = self.encode_text(text, 'cuda')
        
    #     emb = self.time_embed(timestep_embedding(timesteps, self.latent_dim).type(self.clip.dtype)).type(self.clip.dtype) + xf_proj.type(self.clip.dtype)
    #     # print(emb.shape)
        
    #     # B, T, latent_dim
    #     h = self.joint_embed(x)
    #     h = h + self.sequence_embedding.unsqueeze(0)[:, :T, :]
    #     # print(h.shape)
    #     src_mask = self.generate_src_mask(T, length).to('cuda').unsqueeze(-1)
    #     # print(src_mask.shape)
    #     for module in self.temporal_decoder_blocks:
    #         h = module(h, xf_out, emb, src_mask)

    #     output = self.out(h).view(B, T, -1).contiguous()
    #     return output
    def get_Cosine_Similarity_Loss(self):
        # xf_proj = self.weighting_scores(xf_proj, self.weights_scores_signals)
        # text_promts_embed = self.weighting_scores(text_promts_embed,self.weights_scores_promt)
        output = self.xf_proj_af
        target = self.text_promts_embed_af
        output_norm = F.normalize(output, p=2, dim=1)
        target_norm = F.normalize(target, p=2, dim=1)
        
        # Calculate cosine similarity
        cos_sim = torch.sum(output_norm * target_norm, dim=1)
        
        # Convert cosine similarity to cosine distance
        cos_dist = 1 + cos_sim
        
        # Calculate the average loss across the batch
        loss = torch.mean(cos_dist)
        return loss
    def get_MSE_Loss(self):
        # xf_proj = self.weighting_scores(xf_proj, self.weights_scores_signals)
        # text_promts_embed = self.weighting_scores(text_promts_embed,self.weights_scores_promt)
        output = self.xf_proj_af
        target = self.text_promts_embed_af
        l2_loss = ((output - target) ** 2).mean()
        return l2_loss
    def forward(self, x, timesteps ,length=None, text=None, xf_proj=None, xf_out=None):
        """
        x: B, T, D
        """
        # print(length)
        B, T = x.shape[0], x.shape[1]
        # print('---------------')
        # print('xf_proj shape if not none: ' + str(xf_proj.shape))
        # print('xf_out shape if not none: ' + str(xf_out.shape))
        if xf_proj is None or xf_out is None:
            # xf_proj, xf_out = self.encode_text(text, x.device)
            xf_proj, xf_out = self.encode_text(text, 'cuda')
        
       
        emb = self.time_embed(timestep_embedding(timesteps, self.latent_dim).type(self.clip.dtype)).type(self.clip.dtype) + xf_proj.type(self.clip.dtype)
        
        # print(emb.shape)
        
        # B, T, latent_dim
        h = self.joint_embed(x)
        h = h + self.sequence_embedding.unsqueeze(0)[:, :T, :]
        # print(h.shape)
        src_mask = self.generate_src_mask(T, length).to('cuda').unsqueeze(-1)
        # print(src_mask.shape)
        for module in self.temporal_decoder_blocks:
            h = module(h, xf_out, emb, src_mask)

        output = self.out(h).view(B, T, -1).contiguous()
        return output
if __name__ == "__main__":
    doanh = MotionTransformer(253)
    inputs = 'go to school text encoder hi how are you play badminton  hi how are you play badminton  hi how are you play badminton'
    doanh.encode_text_decoding_align(inputs, device = "cpu")