import torch
import os
import re
import logging
from transformers import AutoTokenizer, PretrainedConfig, PreTrainedModel
from transformers.generation.utils import GenerationMixin 
from transformers.modeling_outputs import CausalLMOutputWithPast
import torch.nn as nn
import math

logger = logging.getLogger(__name__)

class MiniMindConfig(PretrainedConfig):
    model_type = "minimind"
    def __init__(self, dim=512, n_layers=8, n_heads=8, vocab_size=6400, max_seq_len=512, **kwargs):
        super().__init__(**kwargs)
        self.dim = dim
        self.n_layers = n_layers
        self.n_heads = n_heads
        self.vocab_size = vocab_size
        self.max_seq_len = max_seq_len
        # 兼容性字段
        self.hidden_size = dim
        self.num_hidden_layers = n_layers
        self.num_attention_heads = n_heads

class RMSNorm(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.eps = 1e-6
        self.weight = nn.Parameter(torch.ones(dim))
    def forward(self, x):
        norm_x = x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps)
        return norm_x * self.weight

class FeedForward(nn.Module):
    def __init__(self, config):
        super().__init__()
        hidden_dim = 4 * config.dim
        self.w1 = nn.Linear(config.dim, hidden_dim, bias=False)
        self.w2 = nn.Linear(hidden_dim, config.dim, bias=False)
        self.w3 = nn.Linear(config.dim, hidden_dim, bias=False)
    def forward(self, x):
        return self.w2(torch.nn.functional.silu(self.w1(x)) * self.w3(x))

class Attention(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.n_heads = config.n_heads
        self.head_dim = config.dim // config.n_heads
        self.wq = nn.Linear(config.dim, config.dim, bias=False)
        self.wk = nn.Linear(config.dim, config.dim, bias=False)
        self.wv = nn.Linear(config.dim, config.dim, bias=False)
        self.wo = nn.Linear(config.dim, config.dim, bias=False)
        self.register_buffer("mask", torch.tril(torch.ones(config.max_seq_len, config.max_seq_len)).view(1, 1, config.max_seq_len, config.max_seq_len))

    def forward(self, x):
        bs, seq_len, _ = x.shape
        q = self.wq(x).view(bs, seq_len, self.n_heads, self.head_dim).transpose(1, 2)
        k = self.wk(x).view(bs, seq_len, self.n_heads, self.head_dim).transpose(1, 2)
        v = self.wv(x).view(bs, seq_len, self.n_heads, self.head_dim).transpose(1, 2)
        
        scores = torch.matmul(q, k.transpose(-2, -1)) / math.sqrt(self.head_dim)
        scores = scores.masked_fill(self.mask[:, :, :seq_len, :seq_len] == 0, float('-inf'))
        probs = torch.softmax(scores, dim=-1)
        output = torch.matmul(probs, v).transpose(1, 2).contiguous().view(bs, seq_len, -1)
        return self.wo(output)

class MiniMindBlock(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.attention = Attention(config)
        self.feed_forward = FeedForward(config)
        self.attention_norm = RMSNorm(config.dim)
        self.ffn_norm = RMSNorm(config.dim)
    def forward(self, x):
        x = x + self.attention(self.attention_norm(x))
        x = x + self.feed_forward(self.ffn_norm(x))
        return x

class MiniMindForCausalLM(PreTrainedModel, GenerationMixin):
    config_class = MiniMindConfig
    def __init__(self, config):
        super().__init__(config)
        self.tok_embeddings = nn.Embedding(config.vocab_size, config.dim)
        self.layers = nn.ModuleList([MiniMindBlock(config) for _ in range(config.n_layers)])
        self.norm = RMSNorm(config.dim)
        self.output = nn.Linear(config.dim, config.vocab_size, bias=False)
        self.output.weight = self.tok_embeddings.weight 

    def forward(self, input_ids, labels=None, **kwargs):
        h = self.tok_embeddings(input_ids)
        for layer in self.layers:
            h = layer(h)
        h = self.norm(h)
        logits = self.output(h)
        
        return CausalLMOutputWithPast(
            loss=None,
            logits=logits,
            past_key_values=None,
            hidden_states=None,
            attentions=None,
        )

    def prepare_inputs_for_generation(self, input_ids, **kwargs):
        return {"input_ids": input_ids}

class LocalLLM:
    def __init__(self, model_dir="./local_model"):
        self.model_dir = model_dir
        self.device = 'cpu' 
        self.model = None
        self.tokenizer = None
        self._load_model()

    def _load_model(self):
        try:
            logger.info(f"📂 [LocalLLM] 正在加载本地模型: {self.model_dir}")
            self.tokenizer = AutoTokenizer.from_pretrained(self.model_dir)
            config = MiniMindConfig()
            if not hasattr(config, 'num_hidden_layers'):
                config.num_hidden_layers = config.n_layers
                config.hidden_size = config.dim
                config.num_attention_heads = config.n_heads
            
            self.model = MiniMindForCausalLM(config).to(self.device)
            weight_path = os.path.join(self.model_dir, "miner_zero.pth")
            
            if not os.path.exists(weight_path):
                logger.error(f"❌ 权重文件不存在: {weight_path}")
                return

            state_dict = torch.load(weight_path, map_location=self.device)
            self.model.load_state_dict(state_dict, strict=False)
            self.model.eval()
            logger.info(f"✅ [LocalLLM] Miner-Zero 加载成功！")
            
        except Exception as e:
            logger.critical(f"❌ [LocalLLM] 模型加载失败: {e}", exc_info=True)

        def generate(self, prompt, temp=1.0):
        if not self.model or not self.tokenizer: return None
        try:
            inputs = self.tokenizer(prompt, return_tensors='pt').to(self.device)
            with torch.no_grad():
                # [v18.9 Tuning] 极度保守模式
                output_ids = self.model.generate(
                    inputs['input_ids'], 
                    max_new_tokens=200, 
                    temperature=0.6,        # [Fix] 降温至 0.6，减少幻觉
                    top_k=20,               # [Fix] 只选概率最高的 20 个词
                    top_p=0.9, 
                    repetition_penalty=1.2, 
                    do_sample=True, 
                    pad_token_id=self.tokenizer.eos_token_id
                )
            full_text = self.tokenizer.decode(output_ids[0], skip_special_tokens=True)
            generated = full_text[len(prompt):].strip()
            
            # [v18.9] 放宽过滤：只要有长度就行
            if len(generated) < 3: 
                return None

            if ";" in generated:
                parts = generated.split(';')
                for p in parts:
                    if len(p) > 3: return p.strip() + ";"
                return parts[0].strip() + ";"
            return generated + ";"
        except Exception as e:
            logger.error(f"生成失败: {e}")
            return None
